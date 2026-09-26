"""層2 の番号候補。

番号体系の落とし方（住居番号 → 街区 → 地番）と、畳んだ町字の両方を引くことは
実データの癖への対処なので、端から端まで通さずここで固定する。偽の索引は
:class:`ports.BanchiSource` を満たすだけの 1 メソッドで済む。
"""

from __future__ import annotations

from jev_abr_geocoder.address import Banchi, BanchiKind, MachiazaName, MachiazaRecord
from jev_abr_geocoder.match import banchi
from jev_abr_geocoder.match.banchi_tail import parse_banchi_tail


class _Source:
    """(lg_code, machiaza_id, kind) -> 番号。問い合わせを記録する。"""

    def __init__(self, data: dict[tuple[int, int, BanchiKind], list[Banchi]]) -> None:
        self._data = data
        self.asked: list[tuple[int, int, BanchiKind]] = []

    def fetch_banchi(
        self, lg_code: int, machiaza_id: int, kind: BanchiKind, *, num1: int | None = None
    ) -> list[Banchi]:
        self.asked.append((lg_code, machiaza_id, kind))
        return list(self._data.get((lg_code, machiaza_id, kind), []))


def _machiaza(*, rsdt: int = 0, alt: tuple[int, ...] = ()) -> MachiazaRecord:
    return MachiazaRecord(
        row_id=0,
        lg_code=312011,
        machiaza_id=1000,
        name=MachiazaName(
            pref="鳥取県",
            county="",
            city="鳥取市",
            ward="",
            oaza_cho="面影",
            chome="",
            chome_number="",
            koaza="",
        ),
        rsdt_addr_flg=rsdt,
        alt_machiaza=alt,
    )


def test_rsdt_town_falls_back_to_parcel() -> None:
    """住居表示の町字でも地番しか無いことがある（全国 1,248 件は両方持つ）。"""
    source = _Source({(312011, 1000, BanchiKind.PARCEL): [Banchi(7, 1)]})
    options = banchi.candidates_for(source, _machiaza(rsdt=1), parse_banchi_tail("7-1"))

    assert options.kind is BanchiKind.PARCEL
    assert [e.numbers for e in options.entries] == [(7, 1)]
    # 住居番号 -> 街区 -> 地番 の順に落とす。
    assert [kind for _lg, _mz, kind in source.asked] == [
        BanchiKind.RSDT,
        BanchiKind.BLOCK,
        BanchiKind.PARCEL,
    ]


def test_exact_rsdt_match_stops_the_fallback() -> None:
    source = _Source({(312011, 1000, BanchiKind.RSDT): [Banchi(1, 2)]})
    options = banchi.candidates_for(source, _machiaza(rsdt=1), parse_banchi_tail("1-2"))

    assert options.kind is BanchiKind.RSDT
    assert [kind for _lg, _mz, kind in source.asked] == [BanchiKind.RSDT]


def test_block_only_input_keeps_the_block_records() -> None:
    """「1番」だけの入力。住居番号に完全一致は無いが街区には在る。"""
    source = _Source(
        {
            (312011, 1000, BanchiKind.RSDT): [Banchi(1, 2)],
            (312011, 1000, BanchiKind.BLOCK): [Banchi(1)],
        }
    )
    options = banchi.candidates_for(source, _machiaza(rsdt=1), parse_banchi_tail("1番"))

    assert options.kind is BanchiKind.BLOCK
    assert [e.numbers for e in options.entries] == [(1,)]


def test_folded_machiaza_is_also_searched() -> None:
    """ABR が同じ場所を 2 レコードに分けたとき、地番も両方に割れている。"""
    source = _Source(
        {
            (312011, 1000, BanchiKind.PARCEL): [Banchi(7)],
            (312011, 2500, BanchiKind.PARCEL): [Banchi(120)],
        }
    )
    town = _machiaza(alt=(2500,))

    near = banchi.candidates_for(source, town, parse_banchi_tail("7番地"))
    far = banchi.candidates_for(source, town, parse_banchi_tail("120番地")).entries

    assert [e.numbers for e in near.entries] == [(7,)]
    assert [e.numbers for e in far] == [(120,)]


def test_representative_is_kept_when_nothing_matches_exactly() -> None:
    """どこにも完全一致が無ければ、最初に見つかった側を返す（親番で拾う余地）。"""
    source = _Source({(312011, 1000, BanchiKind.PARCEL): [Banchi(7, 1)]})
    options = banchi.candidates_for(source, _machiaza(alt=(2500,)), parse_banchi_tail("7番地"))

    assert [e.numbers for e in options.entries] == [(7, 1)]


def test_missing_town_yields_no_options() -> None:
    options = banchi.candidates_for(_Source({}), _machiaza(), parse_banchi_tail("1-2"))
    assert not options
    assert options.entries == ()


def test_parents_finds_branch_numbers() -> None:
    options = banchi.BanchiCandidates(
        entries=(Banchi(936, 1), Banchi(936, 2), Banchi(937)),
        kind=BanchiKind.PARCEL,
    )
    assert [e.numbers for e in banchi.parents(options, (936,))] == [(936, 1), (936, 2)]
    # 完全一致は親番扱いにしない。
    assert banchi.parents(options, (937,)) == ()
    assert banchi.parents(options, ()) == ()


def test_ranked_keeps_the_closest_numbers() -> None:
    options = banchi.BanchiCandidates(
        entries=tuple(Banchi(n) for n in (1, 50, 100, 101)), kind=BanchiKind.PARCEL
    )
    assert [e.num1 for e in banchi.ranked(options, (100,), 2).entries] == [100, 101]
    # 上限内なら並べ替えない。
    assert banchi.ranked(options, (100,), 4) is options
