"""オーケストレーション。

**入力が何件あっても Jev の往復は高々 2 回。** 町字の確定と番号の確定には
依存関係があるので 2 段必要だが、各段ではバッチ全体を 1 リクエストにまとめる。

そのため :meth:`Geocoder.geocode` は :meth:`geocode_many` に委譲するだけで、
単数形の専用経路を持たない。単数経路があると「1 件ずつループで呼ぶ」が自然に
書けてしまい、10 倍遅く 12 倍高くなる（docs/code-design.md 制約1）。

ここにあるのは**段取りだけ**。何を訊くかは :mod:`match.rerank`、番号の引き方は
:mod:`match.numbers`、結果の組み立てと閾値ゲートは :mod:`assemble` にある。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, TypeVar

from . import assemble, ports, textnorm
from .assemble import Resolution
from .config import GeocoderConfig
from .index.townindex import TownIndex
from .match import numbers
from .match.candidates import CandidateFinder, group_by_display, group_by_oaza
from .match.rerank import BeamAsk, NumberAsk, Reranker, TownAsk
from .match.tail import parse_tail
from .models import BatchOutcome, Decision, GeocodeResult, TownCandidate, TownRecord

__all__ = ["Geocoder", "AUTO_MODEL"]

_Ask = TypeVar("_Ask")


class _AutoModel:
    """``model`` 省略時の目印。

    ``None`` は「Jev を使わない」という明示的な指定なので、「指定なし」と
    区別できる必要がある。
    """

    def __repr__(self) -> str:  # pragma: no cover - 表示専用
        return "AUTO_MODEL"


AUTO_MODEL = _AutoModel()


@dataclass(slots=True)
class _Pending(Generic[_Ask]):
    """判定モデルに投げる 1 問と、その答えを書き戻す先。

    問と宛先を別々の平行リストで持つと、片方にだけ足す退行が静かに起きる。
    1 つの型にまとめて添字の対応づけを消す。
    """

    ask: _Ask
    item: Resolution
    #: 町字の段では選択肢ごとにまとめた候補。番号の段では使わない。
    groups: list[list[TownCandidate]] = field(default_factory=list)


class Geocoder:
    def __init__(
        self, index: TownIndex, model: ports.DecisionModel | None, cfg: GeocoderConfig
    ) -> None:
        self._index = index
        self._cfg = cfg
        self._finder = CandidateFinder(index, cfg)
        self._model = model
        self._reranker = Reranker(model, cfg) if model is not None else None

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
        return (await self.run_all(queries)).results

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
        merged = BatchOutcome()
        if not queries:
            return merged
        size = self._cfg.batch_size
        chunks = [queries[i : i + size] for i in range(0, len(queries), size)]
        semaphore = asyncio.Semaphore(max(1, self._cfg.concurrency))

        async def one(chunk: Sequence[str]) -> BatchOutcome:
            async with semaphore:
                return await self.run(chunk)

        for outcome in await asyncio.gather(*(one(chunk) for chunk in chunks)):
            merged.merge(outcome)
        return merged

    async def run(self, queries: Sequence[str]) -> BatchOutcome:
        """**1 バッチ**を処理し、結果と実行統計を返す。

        ここが「入力が何件でも Jev の往復は高々 2 回（beam が要るときだけ 3 回）」
        を満たす単位。件数の多い入力は :meth:`run_all` で区切る。
        """
        outcome = BatchOutcome()
        if not queries:
            return outcome

        items = [self._prepare(query) for query in queries]
        records = self._load_town_records(items)

        await self._narrow(items, records, outcome)
        await self._resolve_towns(items, records, outcome)
        self._load_numbers(items)
        await self._resolve_numbers(items, outcome)

        outcome.results = [assemble.build(item, self._index, self._cfg) for item in items]
        return outcome

    # --------------------------------------------------------------- 段取り

    def _prepare(self, query: str) -> Resolution:
        normalized = textnorm.normalize(query)
        return Resolution(
            query=query,
            normalized=normalized,
            candidates=self._finder.find(normalized),
        )

    def _load_town_records(self, items: Sequence[Resolution]) -> dict[int, TownRecord]:
        """バッチ全体の候補について町字レコードをまとめて引く。"""
        town_ids = {c.town_id for item in items for c in item.candidates.candidates}
        return self._index.store.towns(sorted(town_ids))

    async def _narrow(
        self,
        items: Sequence[Resolution],
        records: dict[int, TownRecord],
        outcome: BatchOutcome,
    ) -> None:
        """候補が Choice の上限を超えた入力だけ、先に大字を決めて絞る。

        町字は「大字 + 丁目 + 小字」なので、**大字だけを選ばせると選択肢が
        一桁以上減る**（福井市 15,399 町字 -> 629 大字、福島市 8,132 -> 205）。
        上限を超える市区町村は町字で数えると 26.0% だが、大字で数えると 4.4%
        まで落ちる。大字名は平均 4.2 文字で、町字フルネームの 16.6 文字に対し
        トークンも 1/4 で済む。

        **この段があるぶん、その入力だけは往復が 3 回になる。** 走るのは
        全体の 0.3% 程度なので、バッチ全体では 1 リクエスト増えるだけ。
        """
        pending: list[_Pending[BeamAsk]] = []
        for item in items:
            if not item.candidates.needs_beam:
                continue
            candidates = [c for c in item.candidates.candidates if c.town_id in records]
            if not candidates or self._reranker is None:
                # 判定モデルが無いなら絞りようがない。粒度を落とす。
                item.candidates = item.candidates.narrowed([])
                continue
            groups = group_by_oaza(candidates, records)
            pending.append(
                _Pending(
                    ask=BeamAsk(
                        query=item.query,
                        normalized=item.normalized,
                        options=[records[g[0].town_id].oaza_display for g in groups],
                    ),
                    item=item,
                    groups=groups,
                )
            )

        if not pending or self._reranker is None:
            return
        result = await self._reranker.narrow([p.ask for p in pending])
        outcome.usage.merge(result.usage)
        outcome.beam_requests += result.usage.requests

        for p, kept in zip(pending, result.survivors, strict=True):
            survivors = [c for i in kept if 0 <= i < len(p.groups) for c in p.groups[i]]
            if not survivors:
                p.item.remember(
                    _failure_note(result.failure) if result.failure else "候補を絞り込めなかった"
                )
            # 大字が決まっても丁目・小字が上限を超えることがある（全国 129,584
            # 組のうち 80 組）。次段が API 制限を破らないようここで収める。
            p.item.candidates = p.item.candidates.narrowed(survivors[: self._cfg.max_candidates])

    async def _resolve_towns(
        self,
        items: Sequence[Resolution],
        records: dict[int, TownRecord],
        outcome: BatchOutcome,
    ) -> None:
        pending: list[_Pending[TownAsk]] = []
        for item in items:
            candidates = [c for c in item.candidates.candidates if c.town_id in records]
            if not candidates:
                continue
            if not self._cfg.always_rerank:
                # 最長一致が一意なら選ぶ余地が無いので Jev を呼ばない。
                # geolonia の難例 7,191 件では、これで Jev 送りが 12.2% から
                # 0.9% に落ちる（同長の競合と曖昧一致だけが残る）。
                unambiguous = item.candidates.unambiguous()
                if unambiguous is not None and unambiguous.town_id in records:
                    _accept(item, records, unambiguous, Decision.fast())
                    outcome.town_fast_path += 1
                    continue
            groups = group_by_display(candidates, records)
            pending.append(
                _Pending(
                    ask=TownAsk(
                        query=item.query,
                        normalized=item.normalized,
                        options=[records[g[0].town_id].display for g in groups],
                    ),
                    item=item,
                    groups=groups,
                )
            )

        if not pending:
            return
        if self._reranker is None:
            # モデルが無い（索引だけで動かしている）場合は候補の先頭。
            for p in pending:
                _accept_top(p, records, "判定モデル未設定のため候補の先頭を採用")
            return

        result = await self._reranker.pick_towns([p.ask for p in pending])
        outcome.usage.merge(result.usage)
        if not result.decisions:
            # 答えが返らなかった。候補の先頭で代替する。
            for p in pending:
                _accept_top(p, records, _failure_note(result.failure))
            return

        for p, decision in zip(pending, result.decisions, strict=True):
            p.item.town_decision = decision
            if decision.index is None or not 0 <= decision.index < len(p.groups):
                continue
            group = p.groups[decision.index]
            _accept(p.item, records, group[0], decision)
            if len(group) > 1:
                # 住所の表記は決まったが、どの machiaza_id かは入力からは
                # 決められない。代表の座標を返す。
                p.item.remember(f"同名の町字が {len(group)} 件あり、座標は代表のもの")

    def _load_numbers(self, items: Sequence[Resolution]) -> None:
        """確定した町字について層2 を引く。1 町字につき BLOB 1 本。"""
        for item in items:
            town, tail = item.town, item.tail
            if town is None or tail is None or not tail.numbers:
                continue
            if not town.from_abr:
                # Geolonia から補った町字は ABR の machiaza_id を持たないので、
                # 層2（街区・住居番号・地番）を引けない。町字で止める。
                continue
            if not item.town_is_confident(self._cfg):
                continue
            item.numbers = numbers.options_for(self._index.store, town, tail)

    async def _resolve_numbers(self, items: Sequence[Resolution], outcome: BatchOutcome) -> None:
        pending: list[_Pending[NumberAsk]] = []
        for item in items:
            options, tail, town = item.numbers, item.tail, item.town
            if options is None or not options or tail is None or town is None:
                continue
            options = numbers.ranked(options, tail.numbers, self._cfg.max_candidates)
            item.numbers = options
            exact = numbers.exact(options, tail.numbers)

            if not exact and not self._cfg.always_rerank and not tail.skipped:
                # **入力が候補の番号列の先頭になっている。** 「中砂見936番地」に
                # 対し ABR は 936-1 / 936-2 / 936-3 しか持たない、という型。
                # 親番は入力どおりで、枝番が分からないだけなので、Jev に
                # 「どの枝番か」を訊いても答えようがない。親番で確定する。
                # 層1 の「大字はあるが丁目付きしか無い」と同じ構造。
                #
                # 数字の手前を読み飛ばしている場合（ABR に無い小字が残って
                # いる等）は町字の解釈が不完全なので、ここでは確定させずに
                # Jev へ回す。
                parents = numbers.parents(options, tail.numbers)
                if parents:
                    item.numbers = options.narrowed(parents)
                    item.number_parent = True
                    item.number_decision = Decision.fast()
                    outcome.number_fast_path += 1
                    continue

            if len(exact) == 1 and not self._cfg.always_rerank:
                # 入力の数値列が実在レコードと完全一致。選ぶ余地が無い。
                item.numbers = options.narrowed(exact)
                item.number_decision = Decision.fast()
                outcome.number_fast_path += 1
                continue

            pending.append(
                _Pending(
                    ask=NumberAsk(
                        query=item.query,
                        town=town.display,
                        tail=tail.raw,
                        kind=options.kind,
                        options=[e.display for e in options.entries],
                    ),
                    item=item,
                )
            )

        if not pending:
            return
        if self._reranker is None:
            for p in pending:
                p.item.number_decision = Decision.unanswered()
            return

        result = await self._reranker.pick_numbers([p.ask for p in pending])
        outcome.usage.merge(result.usage)
        if not result.decisions:
            for p in pending:
                p.item.number_decision = Decision.unanswered()
                p.item.remember(_failure_note(result.failure))
            return
        for p, decision in zip(pending, result.decisions, strict=True):
            p.item.number_decision = decision


# ------------------------------------------------------------------ 補助


def _accept(
    item: Resolution,
    records: dict[int, TownRecord],
    candidate: TownCandidate,
    decision: Decision,
) -> None:
    """町字を採る。残りは数値テールとして解釈する。"""
    item.town = records[candidate.town_id]
    item.tail = parse_tail(candidate.remainder)
    item.town_decision = decision


def _accept_top(p: _Pending[TownAsk], records: dict[int, TownRecord], note: str) -> None:
    """候補の先頭（索引がいちばん近いと見たもの）を採る。

    確信度 0 の :meth:`Decision.unverified` なので、:mod:`assemble` が粒度を
    1 段上げて返す。**モデルの不調で例外を投げない**ための退避路。
    """
    _accept(p.item, records, p.groups[0][0], Decision.unverified())
    p.item.remember(note)


def _failure_note(failure: str) -> str:
    if not failure:
        return "判定モデルが応答しなかったため候補の先頭を採用"
    return f"判定モデルが応答しなかったため候補の先頭を採用: {failure}"
