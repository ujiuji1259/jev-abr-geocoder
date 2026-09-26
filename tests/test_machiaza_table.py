"""町字テーブルの畳み込み。

ABR が同じ場所を複数行で持つ型は 3 つあり（住居表示/地番の 2 行、字あり/なしの
2 行、Geolonia の別表記）、**畳み忘れると判定モデルに見分けのつかない選択肢を
見せる**。行が増えるか鍵だけ増えるかは目で見て分かりにくいので、ここで固定する。
"""

from __future__ import annotations

from collections.abc import Sequence

from jev_abr_geocoder.abr.geolonia import GeoloniaTown
from jev_abr_geocoder.abr.rows import MachiazaRow
from jev_abr_geocoder.address import MachiazaName, Point
from jev_abr_geocoder.index.machiaza_table import MachiazaTable


def _row(
    machiaza_id: int,
    oaza: str,
    *,
    chome: str = "",
    chome_number: str = "",
    koaza: str = "",
    flg: int = 0,
    point: Point | None = None,
    lg_code: int = 312011,
) -> MachiazaRow:
    return MachiazaRow(
        lg_code=lg_code,
        machiaza_id=machiaza_id,
        rsdt_addr_flg=flg,
        name=MachiazaName(
            pref="鳥取県",
            county="",
            city="鳥取市",
            ward="",
            oaza_cho=oaza,
            chome=chome,
            chome_number=chome_number,
            koaza=koaza,
        ),
        point=point,
    )


def _geolonia(town: str, *, lat: float | None = None, lon: float | None = None) -> GeoloniaTown:
    return GeoloniaTown(pref="鳥取県", city="鳥取市", town=town, koaza="", lat=lat, lon=lon)


def _table(rows: Sequence[MachiazaRow]) -> MachiazaTable:
    table = MachiazaTable()
    table.add_abr(rows)
    return table


def _keys_of(table: MachiazaTable, row_id: int) -> set[str]:
    return {key for key, value in table.pairs if value == row_id}


def test_same_machiaza_id_collapses_to_one_row() -> None:
    """住居表示と地番の両方を持つ町字は rsdt_addr_flg 違いの 2 行で来る。"""
    table = _table(
        [
            _row(55001, "面影", chome="一丁目", chome_number="1", flg=0),
            _row(55001, "面影", chome="一丁目", chome_number="1", flg=1, point=Point(35.4, 134.2)),
        ]
    )
    records = table.records

    assert len(records) == 1
    # 住居表示がある側を採る。番号は住居番号 -> 街区 -> 地番 と順に試すので、
    # 1 に寄せても地番しか無い場合は拾える。
    assert records[0].rsdt_addr_flg == 1
    assert records[0].point == Point(35.4, 134.2)
    assert table.stats.folded == 0  # 同じ machiaza_id なので「畳んだ」には数えない


def test_prefix_only_difference_is_the_same_place() -> None:
    """「字青野」と「青野」は同じ住所。地番が両方に割れているので両方引く。"""
    table = _table([_row(2000, "青野"), _row(2500, "字青野")])
    records = table.records

    assert len(records) == 1
    assert records[0].machiaza_ids == (2000, 2500)
    assert table.stats.folded == 1
    # 畳んだ側の表記でも引ける。
    assert "鳥取県鳥取市字青野" in _keys_of(table, 0)
    assert "鳥取県鳥取市青野" in _keys_of(table, 0)


def test_no_key_is_registered_twice() -> None:
    """同じ (鍵, 町字) を二重に登録しない。鍵あたり 1 値で済まなくなる。"""
    table = _table([_row(2000, "青野"), _row(2500, "字青野"), _row(2600, "大字青野")])

    assert len(table.pairs) == len(set(table.pairs))
    assert table.records[0].machiaza_ids == (2000, 2500, 2600)


def test_distinct_towns_stay_distinct() -> None:
    table = _table([_row(55001, "面影", chome="一丁目", chome_number="1"), _row(12000, "叶")])

    assert len(table.records) == 2
    assert table.stats.folded == 0


def test_geolonia_adds_towns_missing_from_abr() -> None:
    """ABR は丁目つきしか持たない大字がある。「柏ケ谷」自体を補う。"""
    table = _table([_row(55001, "面影", chome="一丁目", chome_number="1")])
    table.add_geolonia([_geolonia("吉方", lat=35.5, lon=134.2)])
    records = table.records

    assert len(records) == 2
    added = records[1]
    assert added.source == "geolonia"
    assert not added.from_abr
    # ABR の machiaza_id を持たないので層2 は引けない。出力にも出さない。
    assert added.machiaza_code == ""
    assert added.point == Point(35.5, 134.2)
    assert table.stats.geolonia_added == 1
    assert table.stats.geolonia_merged == 0


def test_geolonia_merges_into_an_existing_town() -> None:
    """同じ場所を ABR が「字新田」、Geolonia が「新田」と持つ型（実測 50,267 件）。

    行を足すと 1 つの鍵が 2 つの町字を指してしまうので、鍵だけ足す。
    """
    table = _table([_row(3000, "古川清水", koaza="字新田")])
    table.add_geolonia([_geolonia("古川清水新田", lat=35.6, lon=134.3)])

    assert len(table.records) == 1
    assert table.stats.geolonia_added == 0
    assert table.stats.geolonia_merged == 1
    # ABR の位置参照は町字をすべて覆っていない。空いていれば Geolonia で埋める。
    assert table.records[0].point == Point(35.6, 134.3)


def test_geolonia_does_not_overwrite_a_known_point() -> None:
    table = _table([_row(3000, "古川清水", koaza="字新田", point=Point(35.1, 134.1))])
    table.add_geolonia([_geolonia("古川清水新田", lat=35.6, lon=134.3)])

    assert table.records[0].point == Point(35.1, 134.1)


def test_geolonia_rows_for_unknown_cities_are_dropped() -> None:
    """市区町村が ABR 側に無ければ lg_code を決められない。"""
    table = _table([_row(55001, "面影")])
    table.add_geolonia(
        [
            GeoloniaTown(
                pref="長崎県", city="佐世保市", town="三川内町", koaza="", lat=33.1, lon=129.8
            )
        ]
    )

    assert len(table.records) == 1
    assert table.stats.geolonia_added == 0


def test_geolonia_matches_across_chome_notation() -> None:
    """ABR は大字と丁目を別列、Geolonia は 1 列。漢数字と算用数字も揺れる。"""
    table = _table([_row(55001, "面影", chome="一丁目", chome_number="1")])
    table.add_geolonia([_geolonia("面影1丁目")])

    assert len(table.records) == 1
    assert table.stats.geolonia_merged == 1


def test_empty_table_reports_zeroes() -> None:
    stats = MachiazaTable().stats
    assert (stats.towns, stats.trie_keys, stats.folded) == (0, 0, 0)
