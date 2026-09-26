"""住所の値型。

ABR が持つ住所そのものを表す型だけを置く。**すべて frozen** で、索引から読んだ
ものを書き換える意味が無いため。名前 (:class:`MachiazaName`) と永続化されるレコード
(:class:`MachiazaRecord`) を分けてあるのは、鍵を作るのに要る列（``chome_number``）が
出力には要らないから。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "Granularity",
    "Point",
    "CityName",
    "MachiazaName",
    "PrefRecord",
    "CityRecord",
    "MachiazaRecord",
    "BanchiKind",
    "Banchi",
]


class Granularity(IntEnum):
    """住所階層のどこまで解決できたか。順序を持つので大小比較に意味がある。"""

    UNKNOWN = 0
    PREF = 1
    CITY = 2
    MACHIAZA = 3  # 大字・町・丁目・小字
    BLOCK = 4  # 街区符号
    RSDT = 5  # 住居番号
    PARCEL = 6  # 地番

    @property
    def label(self) -> str:
        return _LEVEL_LABELS[self]


_LEVEL_LABELS = {
    Granularity.UNKNOWN: "不明",
    Granularity.PREF: "都道府県",
    Granularity.CITY: "市区町村",
    Granularity.MACHIAZA: "町字",
    Granularity.BLOCK: "街区",
    Granularity.RSDT: "住居番号",
    Granularity.PARCEL: "地番",
}


class BanchiKind(IntEnum):
    """層2 のレコード種別。SQLite の主キーの一部でもある。"""

    BLOCK = 1  # mt_rsdtdsp_blk
    RSDT = 2  # mt_rsdtdsp_rsdt
    PARCEL = 3  # mt_parcel

    @property
    def granularity(self) -> Granularity:
        """この体系まで決まったときの粒度。"""
        return _KIND_GRANULARITY[self]


_KIND_GRANULARITY = {
    BanchiKind.BLOCK: Granularity.BLOCK,
    BanchiKind.RSDT: Granularity.RSDT,
    BanchiKind.PARCEL: Granularity.PARCEL,
}


@dataclass(frozen=True, slots=True)
class Point:
    lat: float
    lon: float
    srid: str = "EPSG:6668"


# ------------------------------------------------------------------ 住所の表記

# ``*Name`` は「その粒度を一意に指すのに要る表記のすべて」。市区町村を指すには
# 都道府県が要るので :class:`CityName` は ``pref`` を持ち、町字を指すには市区町村
# が要るので :class:`MachiazaName` は市区町村まで持つ。**住所の文字列を組み立てる
# のはこの 2 つだけの仕事**で、レコードの側は組み立て方を知らない。


@dataclass(frozen=True, slots=True)
class CityName:
    """市区町村の表記。ABR の列をそのまま持つ。"""

    pref: str
    county: str
    city: str
    ward: str

    @property
    def text(self) -> str:
        return f"{self.pref}{self.county}{self.city}{self.ward}"


@dataclass(frozen=True, slots=True)
class MachiazaName:
    """町字の表記。ABR の列をそのまま持つ。

    ``chome_number`` は丁目の算用数字（``chome`` の表記が揺れるため別列で要る）。
    エイリアス鍵を作るのにだけ要るので、永続化も出力もしない。
    """

    pref: str
    county: str
    city: str
    ward: str
    oaza_cho: str
    chome: str
    chome_number: str
    koaza: str

    @property
    def city_name(self) -> CityName:
        return CityName(self.pref, self.county, self.city, self.ward)

    @property
    def city_text(self) -> str:
        """都道府県から区まで  「長崎県北松浦郡佐々町」"""
        return self.city_name.text

    @property
    def machiaza(self) -> str:
        """町字の部分だけ  「面影一丁目」"""
        return f"{self.oaza_cho}{self.chome}{self.koaza}"

    @property
    def display(self) -> str:
        """町字まで通した表記  「鳥取県鳥取市面影一丁目」"""
        return f"{self.city_text}{self.machiaza}"

    @property
    def oaza_display(self) -> str:
        """大字までの表記。丁目・小字を落とした形。

        候補が多すぎる市区町村で、先に大字だけを選ばせるときに使う。
        """
        return f"{self.city_text}{self.oaza_cho}"


# ---------------------------------------------------------------- ABR レコード


@dataclass(frozen=True, slots=True)
class PrefRecord:
    lg_code: int
    pref: str
    point: Point | None = None


@dataclass(frozen=True, slots=True)
class CityRecord:
    """市区町村。``lg_code``（全国地方公共団体コード）が同一性。"""

    lg_code: int
    pref: str
    county: str
    city: str
    ward: str
    point: Point | None = None


@dataclass(frozen=True, slots=True)
class MachiazaRecord:
    """町字。層1 の解決結果であり、層2 を引く鍵でもある。

    表記は :attr:`name` に持たせる。ここに列を平らに並べると
    :class:`MachiazaName` と同じ 7 列を二重に持つことになり、住所を組み立てる
    コードが 2 箇所に分かれる。
    """

    #: 索引の中での通し番号。前方一致トライのペイロードと一致する。
    #: ABR の ``machiaza_id`` とは別物。
    row_id: int
    lg_code: int
    machiaza_id: int
    name: MachiazaName
    rsdt_addr_flg: int
    point: Point | None = None
    #: この行の出所。``"abr"`` か ``"geolonia"``。
    #: geolonia の行は ABR に無い町字を補うもので、machiaza_id を持たない。
    source: str = "abr"
    #: 同じ場所が別の machiaza_id でも収録されている場合の、残りの machiaza_id。
    #:
    #: ABR は同じ町字を「字青野」と「青野」の 2 レコードに分けて持つことが
    #: あり（全国 3,816 組）、**地番が両方に分かれている**。栗原市築館新田は
    #: 字あり側に 324 筆、字なし側に別の 10 筆。片方しか引かないと取りこぼす。
    alt_machiaza: tuple[int, ...] = ()

    @property
    def machiaza_ids(self) -> tuple[int, ...]:
        """層2 を引くべき machiaza_id。代表が先頭。"""
        return (self.machiaza_id, *self.alt_machiaza)

    @property
    def from_abr(self) -> bool:
        return self.source == "abr"

    @property
    def machiaza_code(self) -> str:
        """ABR 表記の 7 桁 machiaza_id。ABR 由来でなければ空。"""
        return f"{self.machiaza_id:07d}" if self.from_abr else ""

    @property
    def lg_code_str(self) -> str:
        """ABR 表記の 6 桁 lg_code。"""
        return f"{self.lg_code:06d}"

    @property
    def banchi_kind(self) -> BanchiKind:
        """この町字で使うべき番号体系。

        住居表示実施区域なら住居番号、そうでなければ地番。
        """
        return BanchiKind.RSDT if self.rsdt_addr_flg == 1 else BanchiKind.PARCEL


@dataclass(frozen=True, slots=True)
class Banchi:
    """層2 の 1 レコード。

    ``num1``/``num2``/``num3`` の意味は :class:`BanchiKind` で変わる。

    ======== ============ ============ ============
    kind     num1         num2         num3
    ======== ============ ============ ============
    BLOCK    blk_num      -            -
    RSDT     blk_num      rsdt_num     rsdt_num2
    PARCEL   prc_num1     prc_num2     prc_num3
    ======== ============ ============ ============

    ABR の ID 列 (``blk_id`` 等) は番号のゼロ詰めなので格納せず、ここで復元する。
    """

    num1: int
    num2: int = 0
    num3: int = 0
    point: Point | None = None

    @property
    def numbers(self) -> tuple[int, ...]:
        """末尾の 0 を落とした番号列。表示と照合に使う。"""
        nums = (self.num1, self.num2, self.num3)
        end = len(nums)
        while end > 1 and nums[end - 1] == 0:
            end -= 1
        return nums[:end]

    @property
    def display(self) -> str:
        return "-".join(str(n) for n in self.numbers)

    def blk_id(self) -> str:
        return f"{self.num1:03d}"

    def rsdt_id(self) -> str:
        return f"{self.num2:03d}"

    def rsdt2_id(self) -> str:
        return f"{self.num3:03d}" if self.num3 else ""

    def prc_id(self) -> str:
        return f"{self.num1:05d}{self.num2:05d}{self.num3:05d}"
