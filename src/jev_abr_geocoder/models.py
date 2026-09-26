"""値型。

入出力と途中結果の型をここに集める。**ABR のレコードと名前は frozen**（索引から
読んだものを書き換える意味が無い）。段を追って埋まる :class:`GeocodeResult` /
:class:`Usage` / :class:`BatchOutcome` だけが可変。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

__all__ = [
    "Level",
    "CityName",
    "TownName",
    "NumberKind",
    "Point",
    "PrefRecord",
    "CityRecord",
    "TownRecord",
    "NumberEntry",
    "TownCandidate",
    "Decision",
    "Usage",
    "BatchOutcome",
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


# ------------------------------------------------------------------ 住所の名前


@dataclass(frozen=True, slots=True)
class CityName:
    """市区町村の名前。ABR の列をそのまま持つ。"""

    pref: str
    county: str
    city: str
    ward: str


@dataclass(frozen=True, slots=True)
class TownName:
    """町字の名前。ABR の列をそのまま持つ。

    ``chome_number`` は丁目の算用数字（``chome`` の表記が揺れるため別列で要る）。
    エイリアス鍵を作るのに使うだけなので、永続化する :class:`TownRecord` には
    含まれない。
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

    **並び順が順位。** 索引に近いものから並び、上限を超えた分は後ろから落とす。
    スコアは持たない。数値を持たせると「この値以上なら採用」と書きたくなり、
    採否の判断が判定モデルからこちら側に漏れる（docs/architecture.md 原則1）。
    """

    town_id: int
    #: 入力のうち町字として消費した部分
    matched: str
    #: 残り（数値テール + 建物名）
    remainder: str


@dataclass(frozen=True, slots=True)
class Decision:
    """判定モデルの結果。閾値との比較はここではなく geocoder が行う。"""

    index: int | None
    probability: float
    confidence: float
    contains_answer: float
    #: モデルを呼ばずに決めた場合 True
    fast_path: bool = False

    @classmethod
    def fast(cls) -> Decision:
        """選ぶ余地が無いので訊かずに決めた。確信度は満点で通す。"""
        return cls(index=0, probability=1.0, confidence=1.0, contains_answer=1.0, fast_path=True)

    @classmethod
    def unverified(cls) -> Decision:
        """モデルに訊けなかったので候補の先頭を採る。

        確信度 0 なので、``geocoder`` は粒度を 1 段上げて返す。
        """
        return cls(index=0, probability=0.0, confidence=0.0, contains_answer=0.0)

    @classmethod
    def unanswered(cls) -> Decision:
        """答えが得られなかった。何も採用しない。"""
        return cls(index=None, probability=0.0, confidence=0.0, contains_answer=0.0)


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0

    def add(self, input_tokens: int, output_tokens: int) -> None:
        """1 リクエスト分を足す。"""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.requests += 1

    def merge(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.requests += other.requests


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

    def merge(self, other: BatchOutcome) -> None:
        """バッチの結果を後ろに繋ぐ。``run_all`` が並行実行の結果を畳む。"""
        self.results.extend(other.results)
        self.town_fast_path += other.town_fast_path
        self.number_fast_path += other.number_fast_path
        self.beam_requests += other.beam_requests
        self.usage.merge(other.usage)
