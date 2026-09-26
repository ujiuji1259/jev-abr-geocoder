"""値型。すべて frozen dataclass で、可変状態を持たない。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

__all__ = [
    "Level",
    "NumberKind",
    "Point",
    "PrefRecord",
    "CityRecord",
    "TownRecord",
    "NumberEntry",
    "TownCandidate",
    "NumberCandidate",
    "Decision",
    "Usage",
    "GeocodeResult",
]


class Level(IntEnum):
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
    Level.UNKNOWN: "不明",
    Level.PREF: "都道府県",
    Level.CITY: "市区町村",
    Level.MACHIAZA: "町字",
    Level.BLOCK: "街区",
    Level.RSDT: "住居番号",
    Level.PARCEL: "地番",
}


class NumberKind(IntEnum):
    """層2 のレコード種別。SQLite の主キーの一部でもある。"""

    BLOCK = 1  # mt_rsdtdsp_blk
    RSDT = 2  # mt_rsdtdsp_rsdt
    PARCEL = 3  # mt_parcel

    @property
    def level(self) -> Level:
        return _KIND_LEVELS[self]


_KIND_LEVELS = {
    NumberKind.BLOCK: Level.BLOCK,
    NumberKind.RSDT: Level.RSDT,
    NumberKind.PARCEL: Level.PARCEL,
}


@dataclass(frozen=True, slots=True)
class Point:
    lat: float
    lon: float
    srid: str = "EPSG:6668"


# ---------------------------------------------------------------- ABR レコード


@dataclass(frozen=True, slots=True)
class PrefRecord:
    lg_code: int
    pref: str
    point: Point | None = None


@dataclass(frozen=True, slots=True)
class CityRecord:
    city_id: int
    lg_code: int
    pref: str
    county: str
    city: str
    ward: str
    point: Point | None = None

    @property
    def display(self) -> str:
        return f"{self.pref}{self.county}{self.city}{self.ward}"


@dataclass(frozen=True, slots=True)
class TownRecord:
    """町字。層1 の解決結果であり、層2 を引く鍵でもある。"""

    town_id: int
    lg_code: int
    machiaza_id: int
    pref: str
    county: str
    city: str
    ward: str
    oaza_cho: str
    chome: str
    koaza: str
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
    def town(self) -> str:
        return f"{self.oaza_cho}{self.chome}{self.koaza}"

    @property
    def display(self) -> str:
        return f"{self.pref}{self.county}{self.city}{self.ward}{self.town}"

    @property
    def oaza_display(self) -> str:
        """大字までの表示。丁目・小字を落とした形。

        候補が多すぎる市区町村で、先に大字だけを選ばせるときに使う。
        """
        return f"{self.pref}{self.county}{self.city}{self.ward}{self.oaza_cho}"

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
    def number_kind(self) -> NumberKind:
        """この町字で使うべき番号体系。

        住居表示実施区域なら住居番号、そうでなければ地番。
        """
        return NumberKind.RSDT if self.rsdt_addr_flg == 1 else NumberKind.PARCEL


@dataclass(frozen=True, slots=True)
class NumberEntry:
    """層2 の 1 レコード。

    ``num1``/``num2``/``num3`` の意味は :class:`NumberKind` で変わる。

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


# ---------------------------------------------------------------- 候補と判定


@dataclass(frozen=True, slots=True)
class TownCandidate:
    """層1 が出した町字候補。

    ``score`` は 255 件に収まらないときに何を落とすかを決めるためだけのもので、
    採否の判断には使わない。判断は Jev が行う（docs/architecture.md 原則1）。
    """

    town_id: int
    matched: str
    remainder: str
    score: float


@dataclass(frozen=True, slots=True)
class NumberCandidate:
    """層2 が出した番号候補。"""

    kind: NumberKind
    entry: NumberEntry
    remainder: str


@dataclass(frozen=True, slots=True)
class Decision:
    """Jev の判定結果。閾値との比較はここではなく geocoder が行う。"""

    index: int | None
    probability: float
    confidence: float
    contains_answer: float
    #: Jev を呼ばずに決めた場合 True
    fast_path: bool = False


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.requests += 1


# ---------------------------------------------------------------- 出力


@dataclass(slots=True)
class GeocodeResult:
    """1 件の正規化結果。

    ``level`` は実際にどこまで解決したか、``resolved`` は要求粒度まで確信を
    持てたかを表す。confidence が閾値未満のとき結果は捨てず、粒度を 1 段上げて
    返すので、この 2 つは独立している。
    """

    query: str
    normalized: str = ""
    level: Level = Level.UNKNOWN
    resolved: bool = False

    pref: str = ""
    county: str = ""
    city: str = ""
    ward: str = ""
    town: str = ""
    number: str = ""
    #: 住所として解釈しなかった残り（建物名・部屋番号など）
    rest: str = ""

    lg_code: str = ""
    machiaza_id: str = ""
    blk_id: str = ""
    rsdt_id: str = ""
    rsdt2_id: str = ""
    prc_id: str = ""

    point: Point | None = None
    confidence: float = 0.0
    probability: float = 0.0
    note: str = ""

    @property
    def address(self) -> str:
        return f"{self.pref}{self.county}{self.city}{self.ward}{self.town}{self.number}"

    @property
    def lat(self) -> float | None:
        return self.point.lat if self.point else None

    @property
    def lon(self) -> float | None:
        return self.point.lon if self.point else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "address": self.address,
            "level": self.level.name.lower(),
            "level_label": self.level.label,
            "resolved": self.resolved,
            "pref": self.pref or None,
            "county": self.county or None,
            "city": self.city or None,
            "ward": self.ward or None,
            "town": self.town or None,
            "number": self.number or None,
            "rest": self.rest or None,
            "lg_code": self.lg_code or None,
            "machiaza_id": self.machiaza_id or None,
            "blk_id": self.blk_id or None,
            "rsdt_id": self.rsdt_id or None,
            "rsdt2_id": self.rsdt2_id or None,
            "prc_id": self.prc_id or None,
            "lat": self.lat,
            "lon": self.lon,
            "confidence": round(self.confidence, 4),
            "probability": round(self.probability, 4),
            "note": self.note or None,
        }


@dataclass(slots=True)
class BatchOutcome:
    """:meth:`Geocoder.geocode_many` の結果と、そのバッチの実行統計。"""

    results: list[GeocodeResult] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    #: Jev を呼ばずに町字が決まった件数
    town_fast_path: int = 0
    #: Jev を呼ばずに番号が決まった件数
    number_fast_path: int = 0
    #: 分割絞り込み (beam) のために増えた往復数
    beam_requests: int = 0
