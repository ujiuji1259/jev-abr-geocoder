"""オーケストレーション。

**入力が何件あっても Jev の往復は高々 2 回。** 町字の確定と番号の確定には
依存関係があるので 2 段必要だが、各段ではバッチ全体を 1 リクエストにまとめる。

そのため :meth:`Geocoder.geocode` は :meth:`geocode_many` に委譲するだけで、
単数形の専用経路を持たない。単数経路があると「1 件ずつループで呼ぶ」が自然に
書けてしまい、10 倍遅く 12 倍高くなる（docs/code-design.md 制約1）。
"""

from __future__ import annotations

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
        return (await self.run(queries)).results

    async def run(self, queries: Sequence[str]) -> BatchOutcome:
        """バッチを処理し、結果と実行統計を返す。"""
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
        ask_candidates: list[list[TownCandidate]] = []

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
            asks.append(
                TownAsk(
                    query=item.query,
                    normalized=item.normalized,
                    options=[records[c.town_id].display for c in candidates],
                )
            )
            ask_items.append(item)
            ask_candidates.append(candidates)

        if asks and self._reranker is not None:
            result = await self._reranker.pick_towns(asks)
            _merge_usage(outcome.usage, result.usage)
            for index, item in enumerate(ask_items):
                candidates = ask_candidates[index]
                decision = result.decisions[index] if index < len(result.decisions) else None
                if decision is None:
                    # Jev が答えを返さなかった。語彙スコア最上位で代替する。
                    item.town = records[candidates[0].town_id]
                    item.tail = parse_tail(candidates[0].remainder)
                    item.result.note = _failure_note(result.failure)
                    item.town_decision = Decision(
                        index=0, probability=0.0, confidence=0.0, contains_answer=0.0
                    )
                    continue
                item.town_decision = decision
                if decision.index is not None and 0 <= decision.index < len(candidates):
                    chosen = candidates[decision.index]
                    item.town = records[chosen.town_id]
                    item.tail = parse_tail(chosen.remainder)
        elif asks:
            # モデルが無い（索引だけで動かしている）場合も語彙スコア最上位。
            for index, item in enumerate(ask_items):
                candidates = ask_candidates[index]
                item.town = records[candidates[0].town_id]
                item.tail = parse_tail(candidates[0].remainder)
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
            if not self._town_is_confident(item):
                continue
            kind = town.number_kind
            entries = self._index.store.fetch_numbers(
                town.lg_code, town.machiaza_id, kind, num1=tail.first
            )
            if not entries and kind is NumberKind.RSDT:
                # 住居表示実施区域でも街区までしか無い町字、また住居表示と地番の
                # 両方を持つ町字（全国 1,248 件）があるので順に落とす。
                for fallback in (NumberKind.BLOCK, NumberKind.PARCEL):
                    entries = self._index.store.fetch_numbers(
                        town.lg_code, town.machiaza_id, fallback, num1=tail.first
                    )
                    if entries:
                        kind = fallback
                        break
            item.entries = entries
            item.kind = kind

    async def _resolve_numbers(self, items: Sequence[_Item], outcome: BatchOutcome) -> None:
        asks: list[NumberAsk] = []
        ask_items: list[_Item] = []
        ask_entries: list[list[NumberEntry]] = []

        for item in items:
            if not item.entries or item.town is None or item.tail is None:
                continue
            entries = _rank_entries(item.entries, item.tail.numbers, self._cfg.max_candidates)
            exact = [e for e in entries if e.numbers == item.tail.numbers]
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
        result.number = _format_number(kind, entry)
        result.level = kind.level
        result.confidence = decision.confidence
        result.probability = decision.probability
        if entry.point is not None:
            result.point = entry.point
        if kind is NumberKind.PARCEL:
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


def _format_number(kind: NumberKind, entry: NumberEntry) -> str:
    if kind is NumberKind.BLOCK:
        return f"{entry.num1}番"
    if kind is NumberKind.RSDT:
        parts = f"{entry.num1}番{entry.num2}号"
        return parts + (f"の{entry.num3}" if entry.num3 else "")
    return f"{entry.display}番地"


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
