"""オーケストレーション。

**入力が何件あっても Jev の往復は高々 2 回。** 町字の確定と番号の確定には
依存関係があるので 2 段必要だが、各段ではバッチ全体を 1 リクエストにまとめる。

そのため :meth:`Geocoder.geocode` は :meth:`geocode_many` に委譲するだけで、
単数形の専用経路を持たない。単数経路があると「1 件ずつループで呼ぶ」が自然に
書けてしまい、10 倍遅く 12 倍高くなる（docs/code-design.md 制約1）。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import textnorm
from .config import GeocoderConfig
from .index.townindex import TownIndex
from .match.candidates import CandidateFinder, CandidateSet
from .match.rerank import BeamAsk, DecisionModel, JevModel, NumberAsk, Reranker, TownAsk
from .match.tail import Tail, parse_tail
from .models import (
    BatchOutcome,
    CityRecord,
    Decision,
    GeocodeResult,
    Level,
    NumberEntry,
    NumberKind,
    TownCandidate,
    TownRecord,
    Usage,
)

__all__ = ["Geocoder", "AUTO_MODEL"]

_DIGITS = re.compile(r"\d+")


class _AutoModel:
    """``model`` 省略時の目印。

    ``None`` は「Jev を使わない」という明示的な指定なので、「指定なし」と
    区別できる必要がある。
    """

    def __repr__(self) -> str:  # pragma: no cover - 表示専用
        return "AUTO_MODEL"


AUTO_MODEL = _AutoModel()


@dataclass(slots=True)
class _Item:
    """1 件分の途中状態。"""

    query: str
    normalized: str
    candidates: CandidateSet
    result: GeocodeResult
    town: TownRecord | None = None
    town_decision: Decision | None = None
    tail: Tail | None = None
    entries: list[NumberEntry] = field(default_factory=list)
    number_decision: Decision | None = None
    kind: NumberKind | None = None
    #: 入力が親番までで、枝番は ABR にしか無い場合 True。
    number_parent: bool = False


class Geocoder:
    def __init__(self, index: TownIndex, model: DecisionModel | None, cfg: GeocoderConfig) -> None:
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
        model: DecisionModel | None | _AutoModel = AUTO_MODEL,
        cfg: GeocoderConfig | None = None,
    ) -> Geocoder:
        """索引を開く。

        ``model`` を省略すると環境変数から Jev クライアントを作る。``None`` を
        明示すると Jev を使わず、トライの候補だけで判定する。
        """
        config = cfg or GeocoderConfig()
        index = TownIndex.open(data_dir)
        resolved = JevModel.from_env(config) if isinstance(model, _AutoModel) else model
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
            merged.results.extend(outcome.results)
            merged.town_fast_path += outcome.town_fast_path
            merged.number_fast_path += outcome.number_fast_path
            merged.beam_requests += outcome.beam_requests
            _merge_usage(merged.usage, outcome.usage)
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
        town_records = self._load_town_records(items)

        await self._narrow(items, town_records, outcome)
        await self._resolve_towns(items, town_records, outcome)
        self._load_numbers(items)
        await self._resolve_numbers(items, outcome)

        outcome.results = [item.result for item in items]
        return outcome

    # --------------------------------------------------------------- 段取り

    def _prepare(self, query: str) -> _Item:
        normalized = textnorm.normalize(query)
        candidates = self._finder.find(normalized)
        return _Item(
            query=query,
            normalized=normalized,
            candidates=candidates,
            result=GeocodeResult(query=query, normalized=normalized),
        )

    def _load_town_records(self, items: Sequence[_Item]) -> dict[int, TownRecord]:
        """バッチ全体の候補について町字レコードをまとめて引く。"""
        town_ids = {candidate.town_id for item in items for candidate in item.candidates.candidates}
        return self._index.store.towns(sorted(town_ids))

    async def _narrow(
        self,
        items: Sequence[_Item],
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
        asks: list[BeamAsk] = []
        ask_items: list[_Item] = []
        ask_groups: list[list[list[TownCandidate]]] = []

        for item in items:
            if not item.candidates.needs_beam:
                continue
            candidates = [c for c in item.candidates.candidates if c.town_id in records]
            if not candidates or self._reranker is None:
                # 判定モデルが無いなら絞りようがない。粒度を落とす。
                item.candidates = _without_candidates(item.candidates)
                continue
            groups = _group_by_oaza(candidates, records)
            asks.append(
                BeamAsk(
                    query=item.query,
                    normalized=item.normalized,
                    options=[records[g[0].town_id].oaza_display for g in groups],
                )
            )
            ask_items.append(item)
            ask_groups.append(groups)

        if not asks or self._reranker is None:
            return
        result = await self._reranker.narrow(asks)
        _merge_usage(outcome.usage, result.usage)
        outcome.beam_requests += result.usage.requests
        for index, item in enumerate(ask_items):
            groups = ask_groups[index]
            kept = result.survivors[index] if index < len(result.survivors) else []
            survivors: list[TownCandidate] = []
            for group_index in kept:
                if 0 <= group_index < len(groups):
                    survivors.extend(groups[group_index])
            if not survivors:
                item.result.note = item.result.note or (
                    _failure_note(result.failure) if result.failure else "候補を絞り込めなかった"
                )
            # 大字が決まっても丁目・小字が上限を超えることがある（全国 129,584
            # 組のうち 80 組）。次段が API 制限を破らないようここで収める。
            item.candidates = _with_candidates(
                item.candidates, survivors[: self._cfg.max_candidates]
            )

    async def _resolve_towns(
        self,
        items: Sequence[_Item],
        records: dict[int, TownRecord],
        outcome: BatchOutcome,
    ) -> None:
        asks: list[TownAsk] = []
        ask_items: list[_Item] = []
        ask_groups: list[list[list[TownCandidate]]] = []

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
                    item.town = records[unambiguous.town_id]
                    item.tail = parse_tail(unambiguous.remainder)
                    item.town_decision = Decision(
                        index=0,
                        probability=1.0,
                        confidence=1.0,
                        contains_answer=1.0,
                        fast_path=True,
                    )
                    outcome.town_fast_path += 1
                    continue
            # **表示が同じ候補を Jev に重ねて見せない。** 京都市中京区には
            # 同名の「大文字町」が 4 つあり、そのまま並べると同一文字列の
            # 選択肢が 4 つ並ぶ。答えようがないので確信度が割れ、住所としては
            # 正しいのに閾値を下回って粒度が落ちていた。
            groups = _group_by_display(candidates, records)
            asks.append(
                TownAsk(
                    query=item.query,
                    normalized=item.normalized,
                    options=[records[g[0].town_id].display for g in groups],
                )
            )
            ask_items.append(item)
            ask_groups.append(groups)

        if asks and self._reranker is not None:
            result = await self._reranker.pick_towns(asks)
            _merge_usage(outcome.usage, result.usage)
            for index, item in enumerate(ask_items):
                groups = ask_groups[index]
                decision = result.decisions[index] if index < len(result.decisions) else None
                if decision is None:
                    # Jev が答えを返さなかった。語彙スコア最上位で代替する。
                    item.town = records[groups[0][0].town_id]
                    item.tail = parse_tail(groups[0][0].remainder)
                    item.result.note = _failure_note(result.failure)
                    item.town_decision = Decision(
                        index=0, probability=0.0, confidence=0.0, contains_answer=0.0
                    )
                    continue
                item.town_decision = decision
                if decision.index is not None and 0 <= decision.index < len(groups):
                    group = groups[decision.index]
                    chosen = group[0]
                    item.town = records[chosen.town_id]
                    item.tail = parse_tail(chosen.remainder)
                    if len(group) > 1:
                        # 住所の表記は決まったが、どの machiaza_id かは
                        # 入力からは決められない。代表の座標を返す。
                        item.result.note = item.result.note or (
                            f"同名の町字が {len(group)} 件あり、座標は代表のもの"
                        )
        elif asks:
            # モデルが無い（索引だけで動かしている）場合も語彙スコア最上位。
            for index, item in enumerate(ask_items):
                first = ask_groups[index][0][0]
                item.town = records[first.town_id]
                item.tail = parse_tail(first.remainder)
                item.town_decision = Decision(
                    index=0, probability=0.0, confidence=0.0, contains_answer=0.0
                )
                item.result.note = "判定モデル未設定のため語彙スコア最上位を採用"

    def _load_numbers(self, items: Sequence[_Item]) -> None:
        """確定した町字について層2 を引く。1 町字につき BLOB 1 本。"""
        for item in items:
            town = item.town
            tail = item.tail
            if town is None or tail is None or not tail.numbers:
                continue
            if not town.from_abr:
                # Geolonia から補った町字は ABR の machiaza_id を持たないので、
                # 層2（街区・住居番号・地番）を引けない。町字で止める。
                continue
            if not self._town_is_confident(item):
                continue
            entries, kind = self._fetch_numbers(town, tail)
            item.entries = entries
            item.kind = kind

    def _fetch_numbers(self, town: TownRecord, tail: Tail) -> tuple[list[NumberEntry], NumberKind]:
        """町字にぶら下がる番号を引く。

        ABR が同じ場所を「字青野」「青野」の 2 レコードに分けている場合、
        **地番も 2 つに割れている**（栗原市築館新田は字あり側 324 筆、字なし側
        に別の 10 筆）。:attr:`TownRecord.machiaza_ids` を順に引き、入力に
        ぴったり合うものが出たところで止める。
        """
        kind = town.number_kind
        best: list[NumberEntry] = []
        best_kind = kind
        for machiaza_id in town.machiaza_ids:
            found, found_kind = self._fetch_one(town.lg_code, machiaza_id, kind, tail)
            if _has_exact(found, tail.numbers):
                return found, found_kind
            if found and not best:
                best, best_kind = found, found_kind
        return best, best_kind

    def _fetch_one(
        self, lg_code: int, machiaza_id: int, kind: NumberKind, tail: Tail
    ) -> tuple[list[NumberEntry], NumberKind]:
        entries = self._index.store.fetch_numbers(lg_code, machiaza_id, kind, num1=tail.first)
        if kind is NumberKind.RSDT and not _has_exact(entries, tail.numbers):
            # 住居表示実施区域でも街区までしか無い町字、住居表示と地番の
            # 両方を持つ町字（全国 1,248 件）、そして「街区だけ入力されて
            # 住居番号が無い」場合があるので順に落とす。
            for fallback in (NumberKind.BLOCK, NumberKind.PARCEL):
                alternative = self._index.store.fetch_numbers(
                    lg_code, machiaza_id, fallback, num1=tail.first
                )
                if _has_exact(alternative, tail.numbers):
                    return alternative, fallback
                entries = entries or alternative
                if entries is alternative and alternative:
                    kind = fallback
        return entries, kind

    async def _resolve_numbers(self, items: Sequence[_Item], outcome: BatchOutcome) -> None:
        asks: list[NumberAsk] = []
        ask_items: list[_Item] = []
        ask_entries: list[list[NumberEntry]] = []

        for item in items:
            if not item.entries or item.town is None or item.tail is None:
                continue
            entries = _rank_entries(item.entries, item.tail.numbers, self._cfg.max_candidates)
            exact = [e for e in entries if e.numbers == item.tail.numbers]
            if not exact and not self._cfg.always_rerank and not item.tail.skipped:
                # **入力が候補の番号列の先頭になっている。** 「中砂見936番地」に
                # 対し ABR は 936-1 / 936-2 / 936-3 しか持たない、という型。
                # 親番は入力どおりで、枝番が分からないだけなので、Jev に
                # 「どの枝番か」を訊いても答えようがない。親番で確定する。
                # 層1 の「大字はあるが丁目付きしか無い」と同じ構造。
                #
                # 数字の手前を読み飛ばしている場合（ABR に無い小字が残って
                # いる等）は町字の解釈が不完全なので、ここでは確定させずに
                # Jev へ回す。
                parents = _parent_matches(entries, item.tail.numbers)
                if parents:
                    item.entries = parents
                    item.number_parent = True
                    item.number_decision = Decision(
                        index=0,
                        probability=1.0,
                        confidence=1.0,
                        contains_answer=1.0,
                        fast_path=True,
                    )
                    outcome.number_fast_path += 1
                    continue
            if len(exact) == 1 and not self._cfg.always_rerank:
                # 入力の数値列が実在レコードと完全一致。選ぶ余地が無い。
                item.entries = exact
                item.number_decision = Decision(
                    index=0, probability=1.0, confidence=1.0, contains_answer=1.0, fast_path=True
                )
                outcome.number_fast_path += 1
                continue
            item.entries = entries
            asks.append(
                NumberAsk(
                    query=item.query,
                    town=item.town.display,
                    tail=item.tail.raw,
                    kind=item.kind or item.town.number_kind,
                    options=[e.display for e in entries],
                )
            )
            ask_items.append(item)
            ask_entries.append(entries)

        if not asks:
            self._assemble(items)
            return

        if self._reranker is None:
            for item in ask_items:
                item.number_decision = Decision(
                    index=None, probability=0.0, confidence=0.0, contains_answer=0.0
                )
            self._assemble(items)
            return

        result = await self._reranker.pick_numbers(asks)
        _merge_usage(outcome.usage, result.usage)
        for index, item in enumerate(ask_items):
            decision = result.decisions[index] if index < len(result.decisions) else None
            if decision is None:
                item.number_decision = Decision(
                    index=None, probability=0.0, confidence=0.0, contains_answer=0.0
                )
                if result.failure and not item.result.note:
                    item.result.note = _failure_note(result.failure)
                continue
            item.number_decision = decision
        self._assemble(items)

    # ------------------------------------------------------------- 組み立て

    def _town_is_confident(self, item: _Item) -> bool:
        decision = item.town_decision
        if item.town is None or decision is None:
            return False
        if decision.fast_path:
            return True
        return (
            decision.confidence >= self._cfg.town_confidence
            and decision.contains_answer >= self._cfg.present_threshold
        )

    def _assemble(self, items: Sequence[_Item]) -> None:
        for item in items:
            self._assemble_one(item)

    def _assemble_one(self, item: _Item) -> None:
        result = item.result
        town = item.town

        if town is None:
            self._fill_coarse(item)
            return

        city = self._index.city_by_lg(town.lg_code)
        if not self._town_is_confident(item):
            # 確信が持てないときは結果を捨てず、粒度を 1 段上げて返す。
            decision = item.town_decision
            self._fill_city(result, town, city)
            if decision is not None:
                # 既に理由が入っている（Jev の不調など）ならそちらを残す。
                result.note = result.note or _low_confidence_note("町字", decision, town.display)
            return

        result.pref = town.pref
        result.county = town.county
        result.city = town.city
        result.ward = town.ward
        result.town = town.town
        result.lg_code = town.lg_code_str
        result.machiaza_id = town.machiaza_code
        result.point = town.point or (city.point if city else None)
        result.level = Level.MACHIAZA
        result.resolved = True
        result.confidence = item.town_decision.confidence if item.town_decision else 0.0
        result.probability = item.town_decision.probability if item.town_decision else 0.0
        result.rest = item.tail.raw if item.tail else ""

        self._fill_number(item, result, town)

    def _fill_number(self, item: _Item, result: GeocodeResult, town: TownRecord) -> None:
        decision = item.number_decision
        if decision is None or not item.entries:
            return
        if decision.index is None or not 0 <= decision.index < len(item.entries):
            if decision.contains_answer and decision.contains_answer < self._cfg.present_threshold:
                result.note = result.note or "番号が候補に見つからない"
            return
        if not decision.fast_path and decision.confidence < self._cfg.number_confidence:
            result.note = result.note or _low_confidence_note(
                "番号", decision, item.entries[decision.index].display
            )
            return

        entry = item.entries[decision.index]
        kind = item.kind or town.number_kind
        numbers = item.tail.numbers if (item.number_parent and item.tail) else entry.numbers
        result.number = _format_number(kind, numbers)
        result.level = _level_for(kind, numbers)
        result.confidence = decision.confidence
        result.probability = decision.probability
        if entry.point is not None:
            result.point = entry.point
        if item.number_parent:
            # 枝番は入力に無いので ABR の ID は付けない。座標は代表のもの。
            result.note = result.note or (
                f"枝番は入力に含まれない。{len(item.entries)} 件のうち代表の座標"
            )
        elif kind is NumberKind.PARCEL:
            result.prc_id = entry.prc_id()
        else:
            result.blk_id = entry.blk_id()
            if kind is NumberKind.RSDT:
                result.rsdt_id = entry.rsdt_id()
                result.rsdt2_id = entry.rsdt2_id()
        result.rest = _rest_after_numbers(item.tail.raw if item.tail else "", entry)

    def _fill_coarse(self, item: _Item) -> None:
        """町字が決まらなかったとき、分かるところまでを返す。"""
        result = item.result
        city_id = item.candidates.city_id
        if city_id is not None:
            city = self._index.city(city_id)
            if city is not None:
                result.pref = city.pref
                result.county = city.county
                result.city = city.city
                result.ward = city.ward
                result.lg_code = f"{city.lg_code:06d}"
                result.point = city.point
                result.level = Level.CITY
                _mark_exhausted(result, item.candidates.exhausted, Level.CITY, "町字")
                return
        lg_code = item.candidates.pref_lg_code
        if lg_code is not None:
            pref = self._index.pref_by_lg(lg_code)
            if pref is not None:
                result.pref = pref.pref
                result.lg_code = f"{pref.lg_code:06d}"
                result.point = pref.point
                result.level = Level.PREF
                _mark_exhausted(result, item.candidates.exhausted, Level.PREF, "市区町村")
                return
        result.level = Level.UNKNOWN
        result.note = result.note or "候補が見つからない"

    def _fill_city(self, result: GeocodeResult, town: TownRecord, city: CityRecord | None) -> None:
        result.pref = town.pref
        result.county = town.county
        result.city = town.city
        result.ward = town.ward
        result.lg_code = town.lg_code_str
        result.point = city.point if city else town.point
        result.level = Level.CITY
        result.resolved = False


# ------------------------------------------------------------------ 補助


def _group_by_display(
    candidates: Sequence[TownCandidate], records: dict[int, TownRecord]
) -> list[list[TownCandidate]]:
    """候補を表示住所ごとにまとめる。出現順を保つ。

    同じ文字列の選択肢を Jev に複数見せても選びようがないので、1 つにまとめる。
    """
    groups: dict[str, list[TownCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(records[candidate.town_id].display, []).append(candidate)
    return list(groups.values())


def _group_by_oaza(
    candidates: Sequence[TownCandidate], records: dict[int, TownRecord]
) -> list[list[TownCandidate]]:
    """候補を大字ごとにまとめる。出現順を保つ。"""
    groups: dict[str, list[TownCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(records[candidate.town_id].oaza_display, []).append(candidate)
    return list(groups.values())


def _with_candidates(base: CandidateSet, candidates: list[TownCandidate]) -> CandidateSet:
    return CandidateSet(
        candidates=candidates,
        exact=base.exact,
        city_id=base.city_id,
        pref_lg_code=base.pref_lg_code,
        exhausted=base.exhausted,
        needs_beam=False,
    )


def _without_candidates(base: CandidateSet) -> CandidateSet:
    return _with_candidates(base, [])


def _merge_usage(target: Usage, source: Usage) -> None:
    target.input_tokens += source.input_tokens
    target.output_tokens += source.output_tokens
    target.requests += source.requests


def _mark_exhausted(
    result: GeocodeResult, exhausted: Level | None, level: Level, missing: str
) -> None:
    """入力がこの粒度で尽きていたなら解決済みとし、そうでなければ理由を残す。"""
    if exhausted is level:
        result.resolved = True
        result.confidence = 1.0
        result.probability = 1.0
    else:
        result.note = result.note or f"{missing}を特定できない"


def _failure_note(failure: str) -> str:
    if not failure:
        return "判定モデルが応答しなかったため語彙スコア最上位を採用"
    return f"判定モデルが応答しなかったため語彙スコア最上位を採用: {failure}"


def _low_confidence_note(what: str, decision: Decision, guess: str) -> str:
    return f"{what}の確信度が低い ({decision.confidence:.2f}): 最有力は {guess}"


def _level_for(kind: NumberKind, numbers: Sequence[int]) -> Level:
    """入力がどこまで特定できたかに応じた粒度。

    住居表示で街区しか与えられていないとき (「面影一丁目1番」) は、住居番号
    ではなく街区として返す。地番は枝番が無くても地番のまま。
    """
    if kind is NumberKind.RSDT and len(tuple(numbers)) < 2:
        return Level.BLOCK
    return kind.level


def _has_exact(entries: Sequence[NumberEntry], numbers: Sequence[int]) -> bool:
    target = tuple(numbers)
    return any(e.numbers == target for e in entries)


def _parent_matches(entries: Sequence[NumberEntry], numbers: Sequence[int]) -> list[NumberEntry]:
    """入力の番号列を先頭に持つ候補。入力が親番までのときに使う。

    「中砂見936番地」(936,) に対して 936-1 / 936-2 / 936-3 が返る。番号の
    並びとしては入力どおりで、枝番が分からないだけなので、どれを選ぶかを
    Jev に訊いても答えようがない。
    """
    target = tuple(numbers)
    if not target:
        return []
    return [e for e in entries if e.numbers[: len(target)] == target and e.numbers != target]


def _format_number(kind: NumberKind, numbers: Sequence[int]) -> str:
    parts: tuple[int, ...] = tuple(numbers)
    if len(parts) == 0:
        return ""
    if kind is NumberKind.BLOCK:
        return f"{parts[0]}番"
    if kind is NumberKind.RSDT:
        if len(parts) < 2:
            return f"{parts[0]}番"
        out = f"{parts[0]}番{parts[1]}号"
        return out + (f"の{parts[2]}" if len(parts) > 2 and parts[2] else "")
    return "-".join(str(n) for n in parts) + "番地"


#: 番号の直後に付く助数詞。採用した番号と一緒に消費する。
_TRAILING_UNITS = ("丁目", "番地", "番", "号室", "号", "地割", "の", "ノ")
_REST_TRIM = "-ー−–—―‐ 　,、"


def _rest_after_numbers(tail: str, entry: NumberEntry) -> str:
    """番号として消費した部分より後ろを残りとして返す。

    どこまでが住所かは Jev が判断済みなので、ここでは採用された番号の個数分だけ
    数値を読み飛ばし、最後の数値に続く助数詞も一緒に落とす。
    """
    pos = 0
    for _ in range(len(entry.numbers)):
        match = _DIGITS.search(tail, pos)
        if match is None:
            return ""
        pos = match.end()
    rest = tail[pos:]
    for unit in _TRAILING_UNITS:
        if rest.startswith(unit):
            rest = rest[len(unit) :]
            break
    return rest.strip(_REST_TRIM)


def _rank_entries(
    entries: Sequence[NumberEntry], numbers: Sequence[int], limit: int
) -> list[NumberEntry]:
    """候補が上限を超える場合に、入力の数値列に近いものを優先して残す。"""
    if len(entries) <= limit:
        return list(entries)

    def distance(entry: NumberEntry) -> tuple[int, int, int]:
        target = tuple(numbers) + (0, 0, 0)
        return (
            abs(entry.num1 - target[0]),
            abs(entry.num2 - target[1]),
            abs(entry.num3 - target[2]),
        )

    return sorted(entries, key=distance)[:limit]
