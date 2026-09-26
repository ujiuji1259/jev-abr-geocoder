"""オーケストレーション。

**入力が何件あっても Jev の往復は高々 2 回。** 町字の確定と番号の確定には
依存関係があるので 2 段必要だが、各段ではバッチ全体を 1 リクエストにまとめる。

そのため :meth:`Geocoder.geocode` は :meth:`geocode_many` に委譲するだけで、
単数形の専用経路を持たない。単数経路があると「1 件ずつループで呼ぶ」が自然に
書けてしまい、10 倍遅く 12 倍高くなる（docs/code-design.md 制約1）。

ここにあるのは**段取りだけ**。何を訊くかは :mod:`match.choose`、番号の引き方は
:mod:`match.banchi`、結果の組み立てと閾値ゲートは :mod:`assemble` にある。

**段は値を書き換えない。** 各段は ``(新しい Resolution の列, その段の統計)`` を
返し、次の段はそれを受ける。途中状態を共有して書き換えると、どの段が何を決めた
のかが読めなくなる（docs/code-design.md 制約5）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from . import assemble, ports, textnorm
from .address import MachiazaRecord
from .assemble import Resolution
from .config import GeocoderConfig
from .decision import Decision, Usage
from .index.machiaza_index import MachiazaIndex
from .match import banchi
from .match.banchi_tail import parse_banchi_tail
from .match.candidates import (
    CandidateFinder,
    MachiazaCandidate,
    group_by_display,
    group_by_oaza,
)
from .match.choose import Chooser, banchi_question, machiaza_question, oaza_question
from .outcome import BatchOutcome, GeocodeResult

__all__ = ["Geocoder", "AUTO_MODEL"]

#: 選択肢ごとにまとめた候補。先頭が代表。
_Groups = tuple[tuple[MachiazaCandidate, ...], ...]


class _AutoModel:
    """``model`` 省略時の目印。

    ``None`` は「Jev を使わない」という明示的な指定なので、「指定なし」と
    区別できる必要がある。
    """

    def __repr__(self) -> str:  # pragma: no cover - 表示専用
        return "AUTO_MODEL"


AUTO_MODEL = _AutoModel()


@dataclass(frozen=True, slots=True)
class _Pending:
    """判定モデルに投げる 1 問と、答えを書き戻す先の位置。

    ``Resolution`` そのものではなく位置で指す。値を書き換えずに差し替えるので、
    参照を持ち回ると古い写しに書いてしまう。
    """

    position: int
    question: ports.Question
    #: 町字の段では選択肢ごとにまとめた候補。番号の段では使わない。
    groups: _Groups = ()


@dataclass(frozen=True, slots=True)
class _Asked:
    """溜めた問への答え。

    ``decisions`` が None なら**訊けなかった** — モデル未設定と、呼んだが答えが
    返らなかった場合の両方。呼び出し側は退避路（候補の先頭 / 答えなし）に落とす。
    """

    decisions: tuple[Decision, ...] | None
    usage: Usage = Usage()
    #: 訊けなかった理由。結果の注記に残す。
    reason: str = ""


class Geocoder:
    def __init__(
        self, index: MachiazaIndex, model: ports.DecisionModel | None, cfg: GeocoderConfig
    ) -> None:
        self._index = index
        self._cfg = cfg
        self._finder = CandidateFinder(index, cfg)
        self._model = model
        self._chooser = Chooser(model, cfg) if model is not None else None

    @classmethod
    def open(
        cls,
        data_dir: Path,
        *,
        model: ports.DecisionModel | None | _AutoModel = AUTO_MODEL,
        cfg: GeocoderConfig | None = None,
    ) -> Geocoder:
        """索引を開く。**ここが合成の根**で、既定のアダプタを選ぶ。

        ``model`` を省略すると環境変数から Jev クライアントを作る。``None`` を
        明示すると判定モデルを使わず、トライの候補だけで決める。
        """
        from . import adapters

        config = cfg or GeocoderConfig()
        index = adapters.open_index(data_dir)
        resolved = adapters.jev_model(config) if isinstance(model, _AutoModel) else model
        return cls(index, resolved, config)

    def close(self) -> None:
        self._index.close()

    def __enter__(self) -> Geocoder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- 公開 API

    async def geocode(self, query: str) -> GeocodeResult:
        results = await self.geocode_many([query])
        return results[0]

    async def geocode_many(self, queries: Sequence[str]) -> list[GeocodeResult]:
        """何件でも受け取り、入力と同じ順で返す。

        ``batch_size`` ごとに区切って **並行に** 処理する。呼び出し側で区切る
        必要はない。
        """
        return list((await self.run_all(queries)).results)

    async def run_all(self, queries: Sequence[str]) -> BatchOutcome:
        """全件を処理し、結果と実行統計を返す。

        ``batch_size`` ごとのバッチに区切り、``concurrency`` 本まで並行に走らせる。

        **処理時間の大半は Jev の応答待ちで、こちらの計算ではない。** 鳥取県の
        法人 20,235 件での実測は、待ち 103.6 秒に対しローカル処理 15.0 秒。
        並行化はそこに直接効く。

        ====== ========= =========
        並行数   所要      1 件あたり
        ====== ========= =========
        1       113.6 秒   5.61 ms
        4        28.9 秒   1.43 ms
        8        16.8 秒   0.83 ms
        ====== ========= =========

        既定を 4 にしてあるのは Jev のレート制限（1,200 リクエスト/分）に
        余裕を持たせるため。8 だと 17 リクエスト/秒に達して上限が近い。
        """
        if not queries:
            return BatchOutcome()
        size = self._cfg.batch_size
        chunks = [queries[i : i + size] for i in range(0, len(queries), size)]
        semaphore = asyncio.Semaphore(max(1, self._cfg.concurrency))

        async def one(chunk: Sequence[str]) -> BatchOutcome:
            async with semaphore:
                return await self.run(chunk)

        merged = BatchOutcome()
        for outcome in await asyncio.gather(*(one(chunk) for chunk in chunks)):
            merged = merged + outcome
        return merged

    async def run(self, queries: Sequence[str]) -> BatchOutcome:
        """**1 バッチ**を処理し、結果と実行統計を返す。

        ここが「入力が何件でも Jev の往復は高々 2 回（分割絞り込みが要るときだけ
        3 回）」を満たす単位。件数の多い入力は :meth:`run_all` で区切る。
        """
        if not queries:
            return BatchOutcome()

        items = [self._prepare(query) for query in queries]
        records = self._load_machiaza(items)

        items, narrowing = await self._narrow(items, records)
        items, machiaza = await self._resolve_machiaza(items, records)
        items = self._load_banchi(items)
        items, numbers = await self._resolve_banchi(items)

        results = tuple(assemble.build(item, self._index, self._cfg) for item in items)
        return narrowing + machiaza + numbers + BatchOutcome(results=results)

    # --------------------------------------------------------------- 段取り

    def _prepare(self, query: str) -> Resolution:
        normalized = textnorm.normalize(query)
        return Resolution(
            query=query,
            normalized=normalized,
            candidates=self._finder.find(normalized),
        )

    def _load_machiaza(self, items: Sequence[Resolution]) -> dict[int, MachiazaRecord]:
        """バッチ全体の候補について町字レコードをまとめて引く。"""
        row_ids = {c.row_id for item in items for c in item.candidates.candidates}
        return self._index.reader.machiaza(sorted(row_ids))

    async def _narrow(
        self, items: Sequence[Resolution], records: dict[int, MachiazaRecord]
    ) -> tuple[list[Resolution], BatchOutcome]:
        """候補が Choice の上限を超えた入力だけ、先に大字を決めて絞る。

        町字は「大字 + 丁目 + 小字」なので、**大字だけを選ばせると選択肢が
        一桁以上減る**（福井市 15,399 町字 -> 629 大字、福島市 8,132 -> 205）。
        上限を超える市区町村は町字で数えると 26.0% だが、大字で数えると 4.4%
        まで落ちる。大字名は平均 4.2 文字で、町字フルネームの 16.6 文字に対し
        トークンも 1/4 で済む。

        **この段があるぶん、その入力だけは往復が 3 回になる。** 走るのは
        全体の 0.3% 程度なので、バッチ全体では 1 リクエスト増えるだけ。
        """
        narrowed: dict[int, Resolution] = {}
        pending: list[_Pending] = []
        for position, item in enumerate(items):
            if not item.candidates.needs_narrowing:
                continue
            candidates = [c for c in item.candidates.candidates if c.row_id in records]
            if not candidates or self._chooser is None:
                # 判定モデルが無いなら絞りようがない。粒度を落とす。
                narrowed[position] = replace(item, candidates=item.candidates.narrowed(()))
                continue
            groups = group_by_oaza(candidates, records)
            pending.append(
                _Pending(
                    position=position,
                    question=oaza_question(
                        item.query,
                        item.normalized,
                        [records[g[0].row_id].name.oaza_display for g in groups],
                    ),
                    groups=groups,
                )
            )

        if not pending or self._chooser is None:
            return _patched(items, narrowed), BatchOutcome()

        result = await self._chooser.narrow([p.question for p in pending])
        for p, kept in zip(pending, result.survivors, strict=True):
            item = items[p.position]
            survivors = tuple(c for i in kept if 0 <= i < len(p.groups) for c in p.groups[i])
            if not survivors:
                item = item.noted(
                    _failure_note(result.failure) if result.failure else "候補を絞り込めなかった"
                )
            # 大字が決まっても丁目・小字が上限を超えることがある（全国 129,584
            # 組のうち 80 組）。次段が API 制限を破らないようここで収める。
            narrowed[p.position] = replace(
                item, candidates=item.candidates.narrowed(survivors[: self._cfg.max_candidates])
            )
        return _patched(items, narrowed), BatchOutcome(
            usage=result.usage, narrow_requests=result.usage.requests
        )

    async def _resolve_machiaza(
        self, items: Sequence[Resolution], records: dict[int, MachiazaRecord]
    ) -> tuple[list[Resolution], BatchOutcome]:
        resolved: dict[int, Resolution] = {}
        pending: list[_Pending] = []
        fast_path = 0

        for position, item in enumerate(items):
            candidates = [c for c in item.candidates.candidates if c.row_id in records]
            if not candidates:
                continue
            if not self._cfg.always_ask:
                # 最長一致が一意なら選ぶ余地が無いので Jev を呼ばない。
                # geolonia の難例 7,191 件では、これで Jev 送りが 12.2% から
                # 0.9% に落ちる（同長の競合と曖昧一致だけが残る）。
                unambiguous = item.candidates.unambiguous()
                if unambiguous is not None and unambiguous.row_id in records:
                    resolved[position] = _accepted(item, records, unambiguous, Decision.fast())
                    fast_path += 1
                    continue
            groups = group_by_display(candidates, records)
            pending.append(
                _Pending(
                    position=position,
                    question=machiaza_question(
                        item.query,
                        item.normalized,
                        [records[g[0].row_id].name.display for g in groups],
                    ),
                    groups=groups,
                )
            )

        asked = await self._ask(pending, unavailable="判定モデル未設定のため候補の先頭を採用")
        if asked.decisions is None:
            # 訊けなかった。候補の先頭で代替する。
            for p in pending:
                resolved[p.position] = _accepted(
                    items[p.position].noted(asked.reason),
                    records,
                    p.groups[0][0],
                    Decision.unverified(),
                )
        else:
            for p, decision in zip(pending, asked.decisions, strict=True):
                resolved[p.position] = _chosen(items[p.position], records, p.groups, decision)

        return _patched(items, resolved), BatchOutcome(
            usage=asked.usage, machiaza_fast_path=fast_path
        )

    def _load_banchi(self, items: Sequence[Resolution]) -> list[Resolution]:
        return [self._with_banchi(item) for item in items]

    def _with_banchi(self, item: Resolution) -> Resolution:
        """確定した町字について層2 を引いた写しを返す。1 町字につき BLOB 1 本。"""
        machiaza, tail = item.machiaza, item.tail
        if machiaza is None or tail is None or not tail.numbers:
            return item
        if not machiaza.from_abr:
            # Geolonia から補った町字は ABR の machiaza_id を持たないので、
            # 層2（街区・住居番号・地番）を引けない。町字で止める。
            return item
        if not item.machiaza_is_confident(self._cfg):
            return item
        return replace(item, banchi=banchi.candidates_for(self._index.reader, machiaza, tail))

    async def _resolve_banchi(
        self, items: Sequence[Resolution]
    ) -> tuple[list[Resolution], BatchOutcome]:
        resolved: dict[int, Resolution] = {}
        pending: list[_Pending] = []
        fast_path = 0

        for position, item in enumerate(items):
            options, tail, machiaza = item.banchi, item.tail, item.machiaza
            if options is None or not options or tail is None or machiaza is None:
                continue
            options = banchi.ranked(options, tail.numbers, self._cfg.max_candidates)
            exact = banchi.exact(options, tail.numbers)

            if not exact and not self._cfg.always_ask and not tail.skipped:
                # **入力が候補の番号列の先頭になっている。** 「中砂見936番地」に
                # 対し ABR は 936-1 / 936-2 / 936-3 しか持たない、という型。
                # 親番は入力どおりで、枝番が分からないだけなので、Jev に
                # 「どの枝番か」を訊いても答えようがない。親番で確定する。
                # 層1 の「大字はあるが丁目付きしか無い」と同じ構造。
                #
                # 数字の手前を読み飛ばしている場合（ABR に無い小字が残って
                # いる等）は町字の解釈が不完全なので、ここでは確定させずに
                # Jev へ回す。
                parents = banchi.parents(options, tail.numbers)
                if parents:
                    resolved[position] = replace(
                        item,
                        banchi=options.narrowed(parents),
                        banchi_parent=True,
                        banchi_decision=Decision.fast(),
                    )
                    fast_path += 1
                    continue

            if len(exact) == 1 and not self._cfg.always_ask:
                # 入力の数値列が実在レコードと完全一致。選ぶ余地が無い。
                resolved[position] = replace(
                    item, banchi=options.narrowed(exact), banchi_decision=Decision.fast()
                )
                fast_path += 1
                continue

            # 上限に収めた候補を次段に渡す。
            resolved[position] = replace(item, banchi=options)
            pending.append(
                _Pending(
                    position=position,
                    question=banchi_question(
                        item.query,
                        machiaza.name.display,
                        tail.raw,
                        [e.display for e in options.entries],
                    ),
                )
            )

        asked = await self._ask(pending)
        for index, p in enumerate(pending):
            decision = Decision.unanswered() if asked.decisions is None else asked.decisions[index]
            resolved[p.position] = replace(resolved[p.position], banchi_decision=decision).noted(
                asked.reason
            )

        return _patched(items, resolved), BatchOutcome(
            usage=asked.usage, banchi_fast_path=fast_path
        )

    async def _ask(self, pending: Sequence[_Pending], *, unavailable: str = "") -> _Asked:
        """溜めた問を 1 リクエストで投げ、答えを問と同じ順で返す。

        段ごとに「モデル未設定」と「無応答」の分岐を書くと、片方で退避を書き忘れる
        退行が起きる。判定は 1 箇所に寄せ、呼び出し側は :class:`_Asked` を見るだけ。
        """
        if not pending:
            return _Asked(decisions=())
        if self._chooser is None:
            return _Asked(decisions=None, reason=unavailable)
        result = await self._chooser.ask([p.question for p in pending])
        if not result.decisions:
            return _Asked(decisions=None, usage=result.usage, reason=_failure_note(result.failure))
        return _Asked(decisions=result.decisions, usage=result.usage)


# ------------------------------------------------------------------ 補助


def _patched(items: Sequence[Resolution], updated: Mapping[int, Resolution]) -> list[Resolution]:
    """決まったものだけ差し替えた新しい列。入力の順は保つ。"""
    if not updated:
        return list(items)
    return [updated.get(position, item) for position, item in enumerate(items)]


def _accepted(
    item: Resolution,
    records: Mapping[int, MachiazaRecord],
    candidate: MachiazaCandidate,
    decision: Decision,
) -> Resolution:
    """町字を採った写しを返す。残りは数値テールとして解釈する。

    ``Decision.unverified()`` で呼ぶと確信度 0 になり、:mod:`assemble` が
    粒度を 1 段上げて返す。**モデルの不調で例外を投げない**ための退避路。
    """
    return replace(
        item,
        machiaza=records[candidate.row_id],
        tail=parse_banchi_tail(candidate.remainder),
        machiaza_decision=decision,
    )


def _chosen(
    item: Resolution,
    records: Mapping[int, MachiazaRecord],
    groups: _Groups,
    decision: Decision,
) -> Resolution:
    """判定モデルが選んだ町字を採った写しを返す。"""
    if decision.index is None or not 0 <= decision.index < len(groups):
        return replace(item, machiaza_decision=decision)
    group = groups[decision.index]
    chosen = _accepted(item, records, group[0], decision)
    if len(group) == 1:
        return chosen
    # 住所の表記は決まったが、どの machiaza_id かは入力からは決められない。
    # 代表の座標を返す。
    return chosen.noted(f"同名の町字が {len(group)} 件あり、座標は代表のもの")


def _failure_note(failure: str) -> str:
    if not failure:
        return "判定モデルが応答しなかったため候補の先頭を採用"
    return f"判定モデルが応答しなかったため候補の先頭を採用: {failure}"
