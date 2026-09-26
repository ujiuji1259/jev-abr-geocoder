"""解決状態から結果を組み立てる。

段（候補生成 → 町字 → 番号）が埋めた :class:`Resolution` を読み、``config.py``
の閾値と突き合わせて :class:`GeocodeResult` を作る。

**確信が持てないときも結果を捨てず、粒度を 1 段上げて返す**のがこの層の仕事。

====================  ==========================================
確信が持てないもの      返すもの
====================  ==========================================
番号                   町字の代表点、``level=MACHIAZA``
町字                   市区町村の代表点、``level=CITY``
市区町村               都道府県の代表点、``level=PREF``
何も当たらない          ``level=UNKNOWN``
====================  ==========================================

``resolved`` は「要求された粒度まで確信を持って解決できたか」、``level`` は
「実際にどこまで解決したか」を表す。この 2 つを分けてあるので、呼び出し側が
「町字まででよい」用途にそのまま使える（docs/code-design.md §7）。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .config import GeocoderConfig
from .index.townindex import TownIndex
from .match.candidates import CandidateSet
from .match.numbers import NumberOptions
from .match.tail import Tail
from .models import (
    CityRecord,
    Decision,
    GeocodeResult,
    Level,
    NumberEntry,
    NumberKind,
    TownRecord,
)

__all__ = ["Resolution", "build"]


@dataclass(slots=True)
class Resolution:
    """1 件分の解決状態。段を追って埋まり、最後に結果へ変換される。"""

    query: str
    normalized: str
    candidates: CandidateSet
    town: TownRecord | None = None
    town_decision: Decision | None = None
    tail: Tail | None = None
    numbers: NumberOptions | None = None
    number_decision: Decision | None = None
    #: 入力が親番までで、枝番は ABR にしか無い場合 True。
    number_parent: bool = False
    #: 解決できなかった理由。
    note: str = ""

    def remember(self, note: str) -> None:
        """理由を残す。**後段の理由で上書きしない。**

        判定モデルが落ちた等の根本的な理由が先に入るので、そちらを残すほうが
        役に立つ。「確信度が低い」はその結果にすぎない。
        """
        if not self.note:
            self.note = note

    def town_is_confident(self, cfg: GeocoderConfig) -> bool:
        """町字を確定したと見なせるか。番号を引きに行くかもこれで決まる。"""
        decision = self.town_decision
        if self.town is None or decision is None:
            return False
        if decision.fast_path:
            return True
        return (
            decision.confidence >= cfg.town_confidence
            and decision.contains_answer >= cfg.present_threshold
        )


def build(item: Resolution, index: TownIndex, cfg: GeocoderConfig) -> GeocodeResult:
    """解決状態を結果にする。"""
    result = GeocodeResult(query=item.query, normalized=item.normalized)
    _fill(result, item, index, cfg)
    # 理由は段の側でも溜まるので、最後にまとめて移す。
    result.note = item.note
    return result


def _fill(result: GeocodeResult, item: Resolution, index: TownIndex, cfg: GeocoderConfig) -> None:
    town = item.town
    if town is None:
        _fill_coarse(result, item, index)
        return

    if not item.town_is_confident(cfg):
        _fill_city(result, town, index.city(town.lg_code))
        if item.town_decision is not None:
            item.remember(_low_confidence_note("町字", item.town_decision, town.display))
        return

    city = index.city(town.lg_code)
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

    _fill_number(result, item, town, cfg)


def _fill_number(
    result: GeocodeResult, item: Resolution, town: TownRecord, cfg: GeocoderConfig
) -> None:
    decision = item.number_decision
    options = item.numbers
    if decision is None or options is None or not options:
        return
    if decision.index is None or not 0 <= decision.index < len(options.entries):
        if decision.contains_answer and decision.contains_answer < cfg.present_threshold:
            item.remember("番号が候補に見つからない")
        return
    entry = options.entries[decision.index]
    if not decision.fast_path and decision.confidence < cfg.number_confidence:
        item.remember(_low_confidence_note("番号", decision, entry.display))
        return

    numbers = item.tail.numbers if (item.number_parent and item.tail) else entry.numbers
    result.number = _format_number(options.kind, numbers)
    result.level = _level_for(options.kind, numbers)
    result.confidence = decision.confidence
    result.probability = decision.probability
    if entry.point is not None:
        result.point = entry.point
    if item.number_parent:
        # 枝番は入力に無いので ABR の ID は付けない。座標は代表のもの。
        item.remember(f"枝番は入力に含まれない。{len(options.entries)} 件のうち代表の座標")
    elif options.kind is NumberKind.PARCEL:
        result.prc_id = entry.prc_id()
    else:
        result.blk_id = entry.blk_id()
        if options.kind is NumberKind.RSDT:
            result.rsdt_id = entry.rsdt_id()
            result.rsdt2_id = entry.rsdt2_id()
    result.rest = _rest_after_numbers(item.tail.raw if item.tail else "", entry)


def _fill_coarse(result: GeocodeResult, item: Resolution, index: TownIndex) -> None:
    """町字が決まらなかったとき、分かるところまでを返す。"""
    lg_code = item.candidates.city_lg_code
    if lg_code is not None:
        city = index.city(lg_code)
        if city is not None:
            result.pref = city.pref
            result.county = city.county
            result.city = city.city
            result.ward = city.ward
            result.lg_code = f"{city.lg_code:06d}"
            result.point = city.point
            result.level = Level.CITY
            _mark_exhausted(result, item, Level.CITY, "町字")
            return

    pref_lg_code = item.candidates.pref_lg_code
    if pref_lg_code is not None:
        pref = index.pref_by_lg(pref_lg_code)
        if pref is not None:
            result.pref = pref.pref
            result.lg_code = f"{pref.lg_code:06d}"
            result.point = pref.point
            result.level = Level.PREF
            _mark_exhausted(result, item, Level.PREF, "市区町村")
            return

    result.level = Level.UNKNOWN
    item.remember("候補が見つからない")


def _fill_city(result: GeocodeResult, town: TownRecord, city: CityRecord | None) -> None:
    """町字までは絞れたが確信が持てない。市区町村として返す。"""
    result.pref = town.pref
    result.county = town.county
    result.city = town.city
    result.ward = town.ward
    result.lg_code = town.lg_code_str
    result.point = city.point if city else town.point
    result.level = Level.CITY
    result.resolved = False


def _mark_exhausted(result: GeocodeResult, item: Resolution, level: Level, missing: str) -> None:
    """入力がこの粒度で尽きていたなら解決済みとし、そうでなければ理由を残す。"""
    if item.candidates.exhausted is level:
        result.resolved = True
        result.confidence = 1.0
        result.probability = 1.0
    else:
        item.remember(f"{missing}を特定できない")


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


_DIGITS = re.compile(r"\d+")
#: 番号の直後に付く助数詞。採用した番号と一緒に消費する。
_TRAILING_UNITS = ("丁目", "番地", "番", "号室", "号", "地割", "の", "ノ")
_REST_TRIM = "-ー−–—―‐ 　,、"


def _rest_after_numbers(tail: str, entry: NumberEntry) -> str:
    """番号として消費した部分より後ろを残りとして返す。

    どこまでが住所かは判定モデルが決めているので、ここでは採用された番号の
    個数分だけ数値を読み飛ばし、最後の数値に続く助数詞も一緒に落とす。
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
