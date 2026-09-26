"""ABR の CSV の列を読む規則。

**列の名前を知っているのはこのファイルだけ。** ``mt_town`` の ``machiaza_id``、
地番の ``prc_id``、代表点の ``rep_lat`` といった列名と意味はここに閉じ、外へは
値型で渡す。ABR の列が増減したときに直す場所が 1 つになる。

状態フラグ 3 の行を落とすのもここ（公式実装 abr-geocoder と同じ扱い）。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..address import BanchiKind, MachiazaName, Point, PrefRecord
from . import csvsrc

__all__ = [
    "CityRow",
    "BanchiRow",
    "MachiazaRow",
    "read_cities",
    "read_banchi_positions",
    "read_banchi",
    "read_prefs",
    "read_simple_positions",
    "read_town_positions",
    "read_machiaza",
]

#: ABR の状態フラグ。3 は取り込まない。
_EXCLUDED_STATUS = "3"

#: 代表点の座標を (lg_code, machiaza_id, rsdt_addr_flg) で引ける形。
MachiazaPositions = Mapping[tuple[int, int, int], Point]


@dataclass(frozen=True, slots=True)
class CityRow:
    """``mt_city`` の 1 行。"""

    lg_code: int
    pref: str
    county: str
    city: str
    ward: str
    point: Point | None


@dataclass(frozen=True, slots=True)
class MachiazaRow:
    """``mt_town`` の 1 行のうち、索引に要る分だけ。"""

    lg_code: int
    machiaza_id: int
    rsdt_addr_flg: int
    name: MachiazaName
    point: Point | None

    @property
    def machiaza_key(self) -> tuple[int, int]:
        """町字の同一性。ABR はこの組を ``rsdt_addr_flg`` 違いで 2 行持つことがある。"""
        return (self.lg_code, self.machiaza_id)


@dataclass(frozen=True, slots=True)
class BanchiRow:
    """街区・住居番号・地番の 1 行。"""

    lg_code: int
    machiaza_id: int
    #: 番号 3 つ組。意味は :class:`BanchiKind` で変わる。
    nums: tuple[int, int, int]
    #: 位置参照拡張と突き合わせるための鍵。**整数に詰めない。**
    record_key: tuple[int, ...]


# ------------------------------------------------------------------ 代表点


def read_simple_positions(paths: Iterable[Path]) -> dict[int, Point]:
    """``mt_pref_pos`` / ``mt_city_pos`` を lg_code で引ける形にする。"""
    out: dict[int, Point] = {}
    for path in paths:
        for row in csvsrc.read_rows(path):
            point = _point(row)
            if point is not None:
                out[int(row["lg_code"])] = point
    return out


def read_town_positions(paths: Iterable[Path]) -> dict[tuple[int, int, int], Point]:
    """``mt_town_pos`` を (lg_code, machiaza_id, rsdt_addr_flg) で引ける形にする。"""
    out: dict[tuple[int, int, int], Point] = {}
    for path in paths:
        for row in csvsrc.read_rows(path):
            point = _point(row)
            if point is not None:
                out[(int(row["lg_code"]), int(row["machiaza_id"]), _flg(row))] = point
    return out


def read_banchi_positions(
    kind: BanchiKind, path: Path | None
) -> dict[int, dict[tuple[int, ...], Point]]:
    """番号の位置参照拡張を lg_code -> 記録鍵 -> 座標 の形にする。"""
    out: dict[int, dict[tuple[int, ...], Point]] = {}
    if path is None:
        return out
    for row in csvsrc.read_rows(path):
        point = _point(row)
        if point is not None:
            out.setdefault(int(row["lg_code"]), {})[_record_key(kind, row)] = point
    return out


# -------------------------------------------------------------------- 本体


def read_prefs(paths: Iterable[Path], positions: Mapping[int, Point]) -> Iterator[PrefRecord]:
    """``mt_pref``。列がそのまま :class:`PrefRecord` なので値型で返す。"""
    for path in paths:
        for row in csvsrc.read_rows(path):
            lg_code = int(row["lg_code"])
            yield PrefRecord(lg_code=lg_code, pref=row["pref"], point=positions.get(lg_code))


def read_cities(paths: Iterable[Path], positions: Mapping[int, Point]) -> Iterator[CityRow]:
    """``mt_city``。廃止された市区町村は落とす。"""
    for path in paths:
        for row in csvsrc.read_rows(path):
            if _excluded(row):
                continue
            lg_code = int(row["lg_code"])
            yield CityRow(
                lg_code=lg_code,
                pref=row["pref"],
                county=row.get("county", ""),
                city=row.get("city", ""),
                ward=row.get("ward", ""),
                point=positions.get(lg_code),
            )


def read_machiaza(paths: Iterable[Path], positions: MachiazaPositions) -> Iterator[MachiazaRow]:
    """``mt_town``。代表点も引き当てて返す。"""
    for path in paths:
        for row in csvsrc.read_rows(path):
            if _excluded(row):
                continue
            lg_code = int(row["lg_code"])
            machiaza_id = int(row["machiaza_id"])
            flg = _flg(row)
            yield MachiazaRow(
                lg_code=lg_code,
                machiaza_id=machiaza_id,
                rsdt_addr_flg=flg,
                name=MachiazaName(
                    pref=row.get("pref", ""),
                    county=row.get("county", ""),
                    city=row.get("city", ""),
                    ward=row.get("ward", ""),
                    oaza_cho=row.get("oaza_cho", ""),
                    chome=row.get("chome", ""),
                    chome_number=row.get("chome_number", ""),
                    koaza=row.get("koaza", ""),
                ),
                # 住居表示と地番で 2 行ある町字は、片方にしか代表点が無いことが
                # ある。自分のフラグで引けなければもう一方でも引く。
                point=(
                    positions.get((lg_code, machiaza_id, flg))
                    or positions.get((lg_code, machiaza_id, 0))
                    or positions.get((lg_code, machiaza_id, 1))
                ),
            )


def read_banchi(kind: BanchiKind, path: Path) -> Iterator[BanchiRow]:
    """``mt_rsdtdsp_blk`` / ``mt_rsdtdsp_rsdt`` / ``mt_parcel``。"""
    for row in csvsrc.read_rows(path):
        if _excluded(row):
            continue
        yield BanchiRow(
            lg_code=int(row["lg_code"]),
            machiaza_id=int(row["machiaza_id"]),
            nums=_nums(kind, row),
            record_key=_record_key(kind, row),
        )


# -------------------------------------------------------------------- 列


def _excluded(row: Mapping[str, str]) -> bool:
    return row.get("status_flg") == _EXCLUDED_STATUS


def _flg(row: Mapping[str, str]) -> int:
    return int(row.get("rsdt_addr_flg") or 0)


def _point(row: Mapping[str, str]) -> Point | None:
    lat, lon = row.get("rep_lat", ""), row.get("rep_lon", "")
    if not lat or not lon:
        return None
    return Point(lat=float(lat), lon=float(lon))


def _nums(kind: BanchiKind, row: Mapping[str, str]) -> tuple[int, int, int]:
    if kind is BanchiKind.BLOCK:
        return int(row.get("blk_num") or 0), 0, 0
    if kind is BanchiKind.RSDT:
        return (
            int(row.get("blk_num") or 0),
            int(row.get("rsdt_num") or 0),
            int(row.get("rsdt_num2") or 0),
        )
    # 地番は prc_num* ではなく prc_id から取る。
    # 「い2」「ﾂ4」のようないろは地番があり、prc_num* は数値とは限らない
    # （鳥取県 2,137,980 筆中 8 件）。prc_id は常に 15 桁の数字で、ABR 自身が
    # それらを符号化した値を持つので、こちらを唯一の出所にする。
    prc_id = row["prc_id"]
    return int(prc_id[0:5]), int(prc_id[5:10]), int(prc_id[10:15])


def _record_key(kind: BanchiKind, row: Mapping[str, str]) -> tuple[int, ...]:
    """本体と位置参照を突き合わせる鍵。ID 列はゼロ詰めなので整数化して使う。

    **タプルのまま使う。** 以前は 1 パート 6 桁の整数に詰めていたが、``prc_id`` は
    15 桁・``machiaza_id`` は 7 桁あるので桁が溢れ、**別の町字の番号と鍵が衝突して
    互いの座標を拾っていた**（鳥取市倭文では地番の半分が町字から 13 km 離れていた）。
    タプルなら桁を考えなくてよい。
    """
    machiaza = int(row["machiaza_id"])
    if kind is BanchiKind.BLOCK:
        return (machiaza, int(row["blk_id"]))
    if kind is BanchiKind.RSDT:
        return (
            machiaza,
            int(row["blk_id"]),
            int(row["rsdt_id"]),
            int(row.get("rsdt2_id") or 0),
        )
    return (machiaza, int(row["prc_id"]))
