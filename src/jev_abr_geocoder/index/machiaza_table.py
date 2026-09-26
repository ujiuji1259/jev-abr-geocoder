"""町字テーブルと索引鍵の組み立て。

ABR の ``mt_town`` は**同じ場所を複数の行で持つ**ことがあり、そのまま取り込むと
判定モデルに見分けのつかない選択肢を見せることになる。答えようがないので確信度
が割れ、住所としては正しいのに閾値を下回って粒度が落ちる。ここが持つのはその
畳み込みの規則と、Geolonia 住所データでの穴埋め。

畳む型は 3 つとも実測に基づく:

1. 住居表示と地番の両方を持つ町字が ``rsdt_addr_flg`` 違いの 2 行（全国 1,248 組）
2. 「字青野」と「青野」のように大字・字の有無だけが違う 2 行（全国 3,816 組）
3. Geolonia が ABR と同じ場所を別表記で持っている（実測 62,166 件中 50,267 件）

2 と 3 では **行を足さずに索引鍵だけを足す**。別行にすると 1 つの鍵が 2 つの
町字を指してしまう。

呼ぶ順は :meth:`MachiazaTable.add_abr` -> :meth:`MachiazaTable.add_geolonia`。後者は
前者が作った市区町村の一覧から ``lg_code`` を引くため。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ..abr.geolonia import GeoloniaTown
from ..abr.rows import MachiazaRow
from ..address import CityName, MachiazaName, MachiazaRecord, Point
from .keys import machiaza_aliases, match_key, without_prefix

__all__ = ["MachiazaTable", "MachiazaStats"]

#: 同じ場所かどうかを見る鍵。大字・字の接頭辞は落としてから比べる。
_Place = tuple[int, str, str, str]


@dataclass(frozen=True, slots=True)
class MachiazaStats:
    """構築の内訳。CLI が報告に出す。"""

    towns: int
    trie_keys: int
    #: ABR 内で同じ場所としてまとめた行数。
    folded: int
    #: ABR に無くて Geolonia から補った町字。
    geolonia_added: int
    #: ABR に既にあったので行を足さず別名だけ足した Geolonia の町字。
    geolonia_merged: int


@dataclass(slots=True)
class _Row:
    """組み立て中の 1 行。畳み込みのあいだだけ可変で持つ。"""

    row_id: int
    lg_code: int
    machiaza_id: int
    name: MachiazaName
    rsdt_addr_flg: int
    point: Point | None
    source: str = "abr"
    #: 同じ場所の別レコードの machiaza_id。
    alt_machiaza: list[int] = field(default_factory=list)

    def to_record(self) -> MachiazaRecord:
        return MachiazaRecord(
            row_id=self.row_id,
            lg_code=self.lg_code,
            machiaza_id=self.machiaza_id,
            name=self.name,
            rsdt_addr_flg=self.rsdt_addr_flg,
            point=self.point,
            source=self.source,
            alt_machiaza=tuple(self.alt_machiaza),
        )


class MachiazaTable:
    """町字の行と (索引鍵, row_id) の組を溜める。"""

    def __init__(self) -> None:
        self._rows: list[_Row] = []
        self._pairs: list[tuple[str, int]] = []
        #: (lg_code, machiaza_id) -> 行の添字
        self._by_machiaza: dict[tuple[int, int], int] = {}
        #: 大字・字を落とした名前 -> 行の添字
        self._by_place: dict[_Place, int] = {}
        #: 別名を足した先が既に持っている鍵。同じ (鍵, 町字) を二重に登録しない。
        self._grown: dict[int, set[str]] = {}
        self._folded = 0
        self._geolonia_added = 0
        self._geolonia_merged = 0

    @property
    def records(self) -> list[MachiazaRecord]:
        return [row.to_record() for row in self._rows]

    @property
    def pairs(self) -> list[tuple[str, int]]:
        """(索引鍵, row_id)。同じ鍵が複数の町字を指すことはある（同名の町字）。"""
        return self._pairs

    @property
    def stats(self) -> MachiazaStats:
        return MachiazaStats(
            towns=len(self._rows),
            trie_keys=len(self._pairs),
            folded=self._folded,
            geolonia_added=self._geolonia_added,
            geolonia_merged=self._geolonia_merged,
        )

    # ------------------------------------------------------------- ABR

    def add_abr(self, rows: Iterable[MachiazaRow]) -> None:
        for row in rows:
            aliases = machiaza_aliases(row.name)
            if not aliases:
                continue

            twin = self._by_machiaza.get(row.machiaza_key)
            if twin is not None:
                # 同じ machiaza_id の 2 行目。行は足さない。
                self._absorb(twin, row)
                continue

            place = _place_of(row.lg_code, row.name)
            twin = self._by_place.get(place)
            if twin is not None:
                # 同じ場所の別レコード。栗原市築館青野で確認したところ、両側の
                # 地番は空間的に隣接していて番号帯だけが 1..11 と 42..101 に
                # 割れていた。鳥取県の 88 組では片側の地番が 0 件（幽霊）。
                # **両方の machiaza_id を持たせて層2 は両方引く。**
                self._rows[twin].alt_machiaza.append(row.machiaza_id)
                self._absorb(twin, row)
                self._by_machiaza[row.machiaza_key] = twin
                self._grow(twin, aliases)
                self._folded += 1
                continue

            index = self._append(
                row.lg_code,
                row.machiaza_id,
                row.name,
                aliases,
                rsdt_addr_flg=row.rsdt_addr_flg,
                point=row.point,
            )
            self._by_machiaza[row.machiaza_key] = index
            self._by_place[place] = index

    def _absorb(self, index: int, row: MachiazaRow) -> None:
        """畳む先に、行を足さずに属性だけ取り込む。

        住居表示がある側の ``rsdt_addr_flg`` を採る。番号の取得は
        住居番号 -> 街区 -> 地番 と順に試すので、1 に寄せても地番しか無い場合は
        拾える。代表点は ABR 側が片方にしか入れていないことがあるので埋める。
        """
        kept = self._rows[index]
        kept.rsdt_addr_flg = max(kept.rsdt_addr_flg, row.rsdt_addr_flg)
        if kept.point is None:
            kept.point = row.point

    # -------------------------------------------------------- Geolonia

    def add_geolonia(self, towns: Iterable[GeoloniaTown]) -> None:
        """ABR に無い町字を Geolonia 住所データから補う。

        ABR には **丁目や小字を持つ大字について、大字そのものの行が無い**ことが
        ある。「海老名市柏ケ谷」は一丁目〜六丁目しか無く、入力の番地が旧地番の
        ときに町字を決められない。実測で 3,882 件がこの型、さらに 3,246 件は
        ABR に大字ごと無い。

        補った行は ``machiaza_id`` を持たないので層2（街区・住居番号・地番）は
        引けない。町字までで止まり、残りは未解決部分として返る。
        """
        # ABR 側の照合表。同じ鍵に複数の行が当たるときは先に来た行を採る。
        by_place: dict[str, int] = {}
        cities: dict[str, tuple[int, CityName]] = {}
        for row in self._rows:
            city = row.name.city_text
            name = row.name
            by_place.setdefault(match_key(city, name.oaza_cho, name.chome, name.koaza), row.row_id)
            # geolonia は市区町村コードを持つが ABR の lg_code とは桁が違うので
            # 名前で引く。
            cities.setdefault(match_key(city), (row.lg_code, name.city_name))

        for town in towns:
            found = cities.get(match_key(town.pref + town.city))
            if found is None:
                continue
            lg_code, city = found
            name = MachiazaName(
                pref=city.pref,
                county=city.county,
                city=city.city,
                ward=city.ward,
                oaza_cho=town.town,
                chome="",
                chome_number="",
                koaza=town.koaza,
            )
            aliases = machiaza_aliases(name)
            if not aliases:
                continue

            key = match_key(town.pref + town.city, town.town, "", town.koaza)
            owner = by_place.get(key)
            if owner is not None:
                # 同じ場所。行は足さず、この表記の鍵だけ ABR の町字に向ける。
                self._grow(owner, aliases)
                self._geolonia_merged += 1
                # ABR の位置参照は町字をすべては覆っていない。空いていれば埋める。
                held = self._rows[owner]
                if held.point is None:
                    held.point = _point_of(town)
                continue

            index = self._append(
                lg_code,
                # ABR の machiaza_id を持たない。層2 は引けない。
                self._geolonia_added,
                name,
                aliases,
                point=_point_of(town),
                source="geolonia",
            )
            by_place[key] = index
            self._geolonia_added += 1

    # ----------------------------------------------------------- 共通

    def _append(
        self,
        lg_code: int,
        machiaza_id: int,
        name: MachiazaName,
        aliases: set[str],
        *,
        rsdt_addr_flg: int = 0,
        point: Point | None = None,
        source: str = "abr",
    ) -> int:
        index = len(self._rows)
        self._rows.append(
            _Row(
                row_id=index,
                lg_code=lg_code,
                machiaza_id=machiaza_id,
                name=name,
                rsdt_addr_flg=rsdt_addr_flg,
                point=point,
                source=source,
            )
        )
        self._pairs.extend((alias, index) for alias in aliases)
        return index

    def _grow(self, index: int, aliases: set[str]) -> None:
        """畳んだ先に別名を足す。**同じ (鍵, 町字) を二重に登録しない。**

        畳んだ分だけしか作らないので、持ち歩く集合は小さい。
        """
        already = self._grown.setdefault(index, machiaza_aliases(self._rows[index].name))
        self._pairs.extend((alias, index) for alias in aliases - already)
        already |= aliases


def _place_of(lg_code: int, name: MachiazaName) -> _Place:
    return (lg_code, without_prefix(name.oaza_cho), name.chome, without_prefix(name.koaza))


def _point_of(town: GeoloniaTown) -> Point | None:
    if town.lat is None or town.lon is None:
        return None
    return Point(lat=town.lat, lon=town.lon)
