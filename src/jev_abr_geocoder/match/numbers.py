"""層2 の番号候補。

町字が決まったあと、入力の数値テールに対応する番号を索引から引く。ここが持つ
のは **どの番号体系をどの順に試すか**と**候補をどう絞るか**だけで、どれが正解
かの判断はしない（それは判定モデルと ``geocoder`` の閾値の仕事）。

ABR の番号は町字ごとに 1 つの BLOB になっているので、引くのは 1 町字 1 回。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .. import ports
from ..models import NumberEntry, NumberKind, TownRecord
from .tail import Tail

__all__ = ["NumberOptions", "options_for", "exact", "parents", "ranked"]


@dataclass(frozen=True, slots=True)
class NumberOptions:
    """町字配下の番号候補。

    ``kind`` は町字の既定の体系ではなく、**実際に候補が見つかった体系**。
    住居表示の町字でも街区や地番しか無いことがあるため、両者は一致しない。
    """

    entries: list[NumberEntry]
    kind: NumberKind

    def __bool__(self) -> bool:
        return bool(self.entries)

    def narrowed(self, entries: list[NumberEntry]) -> NumberOptions:
        """候補を絞った同じ体系の候補集合。"""
        return NumberOptions(entries=entries, kind=self.kind)


def options_for(store: ports.NumberSource, town: TownRecord, tail: Tail) -> NumberOptions:
    """町字にぶら下がる番号を引く。

    ABR が同じ場所を「字青野」「青野」の 2 レコードに分けている場合、
    **地番も 2 つに割れている**（栗原市築館新田は字あり側 324 筆、字なし側に
    別の 10 筆）。:attr:`TownRecord.machiaza_ids` を順に引き、入力にぴったり
    合うものが出たところで止める。
    """
    kind = town.number_kind
    best = NumberOptions(entries=[], kind=kind)
    for machiaza_id in town.machiaza_ids:
        found = _for_machiaza(store, town.lg_code, machiaza_id, kind, tail)
        if _has_exact(found.entries, tail.numbers):
            return found
        if found and not best:
            best = found
    return best


def _for_machiaza(
    store: ports.NumberSource, lg_code: int, machiaza_id: int, kind: NumberKind, tail: Tail
) -> NumberOptions:
    entries = store.fetch_numbers(lg_code, machiaza_id, kind, num1=tail.first)
    if kind is not NumberKind.RSDT or _has_exact(entries, tail.numbers):
        return NumberOptions(entries=entries, kind=kind)
    # 住居表示実施区域でも街区までしか無い町字、住居表示と地番の両方を持つ
    # 町字（全国 1,248 件）、そして「街区だけ入力されて住居番号が無い」場合が
    # あるので順に落とす。
    for fallback in (NumberKind.BLOCK, NumberKind.PARCEL):
        alternative = store.fetch_numbers(lg_code, machiaza_id, fallback, num1=tail.first)
        if _has_exact(alternative, tail.numbers):
            return NumberOptions(entries=alternative, kind=fallback)
        if not entries and alternative:
            entries, kind = alternative, fallback
    return NumberOptions(entries=entries, kind=kind)


def exact(options: NumberOptions, numbers: Sequence[int]) -> list[NumberEntry]:
    """入力の数値列と完全に一致する候補。"""
    target = tuple(numbers)
    return [e for e in options.entries if e.numbers == target]


def parents(options: NumberOptions, numbers: Sequence[int]) -> list[NumberEntry]:
    """入力の番号列を先頭に持つ候補。入力が親番までのときに使う。

    「中砂見936番地」(936,) に対して 936-1 / 936-2 / 936-3 が返る。番号の
    並びとしては入力どおりで、枝番が分からないだけなので、どれを選ぶかを
    判定モデルに訊いても答えようがない。
    """
    target = tuple(numbers)
    if not target:
        return []
    return [
        e for e in options.entries if e.numbers[: len(target)] == target and e.numbers != target
    ]


def ranked(options: NumberOptions, numbers: Sequence[int], limit: int) -> NumberOptions:
    """候補が上限を超える場合に、入力の数値列に近いものを優先して残す。"""
    if len(options.entries) <= limit:
        return options

    target = tuple(numbers) + (0, 0, 0)

    def distance(entry: NumberEntry) -> tuple[int, int, int]:
        return (
            abs(entry.num1 - target[0]),
            abs(entry.num2 - target[1]),
            abs(entry.num3 - target[2]),
        )

    return options.narrowed(sorted(options.entries, key=distance)[:limit])


def _has_exact(entries: Sequence[NumberEntry], numbers: Sequence[int]) -> bool:
    target = tuple(numbers)
    return any(e.numbers == target for e in entries)
