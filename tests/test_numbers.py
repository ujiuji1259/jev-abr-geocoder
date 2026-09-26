"""層2 の番号候補。

番号体系の落とし方（住居番号 → 街区 → 地番）と、畳んだ町字の両方を引くことは
実データの癖への対処なので、端から端まで通さずここで固定する。偽の索引は
:class:`ports.NumberSource` を満たすだけの 1 メソッドで済む。
"""

from __future__ import annotations

from jev_abr_geocoder.match import numbers
from jev_abr_geocoder.match.tail import parse_tail
from jev_abr_geocoder.models import NumberEntry, NumberKind, TownRecord


class _Source:
    """(lg_code, machiaza_id, kind) -> 番号。問い合わせを記録する。"""

    def __init__(self, data: dict[tuple[int, int, NumberKind], list[NumberEntry]]) -> None:
        self._data = data
        self.asked: list[tuple[int, int, NumberKind]] = []

    def fetch_numbers(
        self, lg_code: int, machiaza_id: int, kind: NumberKind, *, num1: int | None = None
    ) -> list[NumberEntry]:
        self.asked.append((lg_code, machiaza_id, kind))
        return list(self._data.get((lg_code, machiaza_id, kind), []))


def _town(*, rsdt: int = 0, alt: tuple[int, ...] = ()) -> TownRecord:
    return TownRecord(
        town_id=0,
        lg_code=312011,
        machiaza_id=1000,
        pref="鳥取県",
        county="",
        city="鳥取市",
        ward="",
        oaza_cho="面影",
        chome="",
        koaza="",
        rsdt_addr_flg=rsdt,
        alt_machiaza=alt,
    )


def test_rsdt_town_falls_back_to_parcel() -> None:
    """住居表示の町字でも地番しか無いことがある（全国 1,248 件は両方持つ）。"""
    source = _Source({(312011, 1000, NumberKind.PARCEL): [NumberEntry(7, 1)]})
    options = numbers.options_for(source, _town(rsdt=1), parse_tail("7-1"))

    assert options.kind is NumberKind.PARCEL
    assert [e.numbers for e in options.entries] == [(7, 1)]
    # 住居番号 -> 街区 -> 地番 の順に落とす。
    assert [kind for _lg, _mz, kind in source.asked] == [
        NumberKind.RSDT,
        NumberKind.BLOCK,
        NumberKind.PARCEL,
    ]


def test_exact_rsdt_match_stops_the_fallback() -> None:
    source = _Source({(312011, 1000, NumberKind.RSDT): [NumberEntry(1, 2)]})
    options = numbers.options_for(source, _town(rsdt=1), parse_tail("1-2"))

    assert options.kind is NumberKind.RSDT
    assert [kind for _lg, _mz, kind in source.asked] == [NumberKind.RSDT]


def test_block_only_input_keeps_the_block_records() -> None:
    """「1番」だけの入力。住居番号に完全一致は無いが街区には在る。"""
    source = _Source(
        {
            (312011, 1000, NumberKind.RSDT): [NumberEntry(1, 2)],
            (312011, 1000, NumberKind.BLOCK): [NumberEntry(1)],
        }
    )
    options = numbers.options_for(source, _town(rsdt=1), parse_tail("1番"))

    assert options.kind is NumberKind.BLOCK
    assert [e.numbers for e in options.entries] == [(1,)]


def test_folded_machiaza_is_also_searched() -> None:
    """ABR が同じ場所を 2 レコードに分けたとき、地番も両方に割れている。"""
    source = _Source(
        {
            (312011, 1000, NumberKind.PARCEL): [NumberEntry(7)],
            (312011, 2500, NumberKind.PARCEL): [NumberEntry(120)],
        }
    )
    town = _town(alt=(2500,))

    near = numbers.options_for(source, town, parse_tail("7番地"))
    far = numbers.options_for(source, town, parse_tail("120番地")).entries

    assert [e.numbers for e in near.entries] == [(7,)]
    assert [e.numbers for e in far] == [(120,)]


def test_representative_is_kept_when_nothing_matches_exactly() -> None:
    """どこにも完全一致が無ければ、最初に見つかった側を返す（親番で拾う余地）。"""
    source = _Source({(312011, 1000, NumberKind.PARCEL): [NumberEntry(7, 1)]})
    options = numbers.options_for(source, _town(alt=(2500,)), parse_tail("7番地"))

    assert [e.numbers for e in options.entries] == [(7, 1)]


def test_missing_town_yields_no_options() -> None:
    options = numbers.options_for(_Source({}), _town(), parse_tail("1-2"))
    assert not options
    assert options.entries == []


def test_parents_finds_branch_numbers() -> None:
    options = numbers.NumberOptions(
        entries=[NumberEntry(936, 1), NumberEntry(936, 2), NumberEntry(937)],
        kind=NumberKind.PARCEL,
    )
    assert [e.numbers for e in numbers.parents(options, (936,))] == [(936, 1), (936, 2)]
    # 完全一致は親番扱いにしない。
    assert numbers.parents(options, (937,)) == []
    assert numbers.parents(options, ()) == []


def test_ranked_keeps_the_closest_numbers() -> None:
    options = numbers.NumberOptions(
        entries=[NumberEntry(n) for n in (1, 50, 100, 101)], kind=NumberKind.PARCEL
    )
    assert [e.num1 for e in numbers.ranked(options, (100,), 2).entries] == [100, 101]
    # 上限内なら並べ替えない。
    assert numbers.ranked(options, (100,), 4) is options
