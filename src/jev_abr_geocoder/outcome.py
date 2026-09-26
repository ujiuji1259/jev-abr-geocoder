"""出力の値型。

``geocode_many`` が返すもの。:class:`GeocodeResult` は 1 件分、
:class:`BatchOutcome` はそれとバッチ全体の実行統計。

**どちらも frozen。** 結果を受け取った側が書き換えられないので、キャッシュに
入れても複数のワーカーに渡しても安全。組み立ての途中は :mod:`assemble` が
``dataclasses.replace`` で新しい値を作りながら進める（docs/code-design.md 制約5）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .address import Granularity, Point
from .decision import Usage

__all__ = ["GeocodeResult", "BatchOutcome"]


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    """1 件の正規化結果。

    ``granularity`` は実際にどこまで解決したか、``resolved`` は要求粒度まで確信を
    持てたかを表す。confidence が閾値未満のとき結果は捨てず、粒度を 1 段上げて
    返すので、この 2 つは独立している。
    """

    query: str
    normalized: str = ""
    granularity: Granularity = Granularity.UNKNOWN
    resolved: bool = False

    pref: str = ""
    county: str = ""
    city: str = ""
    ward: str = ""
    machiaza: str = ""
    banchi: str = ""
    #: 住所として解釈しなかった残り（建物名・部屋番号など）
    remainder: str = ""

    lg_code: str = ""
    machiaza_id: str = ""
    blk_id: str = ""
    rsdt_id: str = ""
    rsdt2_id: str = ""
    prc_id: str = ""

    point: Point | None = None
    #: 座標が**何の代表点か**。``granularity`` より粗いことがある。
    #:
    #: ABR は町字の代表点をすべて持っておらず（全国 730,807 町字のうち 442,178 件、
    #: うち 96% は小字レベル）、無いときは市区町村の代表点で代用する。そのとき
    #: ``granularity`` は町字のままなので、**これが無いと呼び出し側から代用を
    #: 見分けられない**。郵便番号データ 3,000 件では 5.3% がこの状態だった。
    point_granularity: Granularity = Granularity.UNKNOWN
    confidence: float = 0.0
    probability: float = 0.0
    note: str = ""

    @property
    def address(self) -> str:
        return f"{self.pref}{self.county}{self.city}{self.ward}{self.machiaza}{self.banchi}"

    @property
    def point_is_coarser(self) -> bool:
        """座標が答えより粗い代表点か。地図に置く用途ではここで弾く。"""
        return self.point is not None and self.point_granularity < self.granularity

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
            "granularity": self.granularity.name.lower(),
            "granularity_label": self.granularity.label,
            "resolved": self.resolved,
            "pref": self.pref or None,
            "county": self.county or None,
            "city": self.city or None,
            "ward": self.ward or None,
            "machiaza": self.machiaza or None,
            "banchi": self.banchi or None,
            "remainder": self.remainder or None,
            "lg_code": self.lg_code or None,
            "machiaza_id": self.machiaza_id or None,
            "blk_id": self.blk_id or None,
            "rsdt_id": self.rsdt_id or None,
            "rsdt2_id": self.rsdt2_id or None,
            "prc_id": self.prc_id or None,
            "lat": self.lat,
            "lon": self.lon,
            "point_granularity": self.point_granularity.name.lower(),
            "confidence": round(self.confidence, 4),
            "probability": round(self.probability, 4),
            "note": self.note or None,
        }


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """:meth:`Geocoder.geocode_many` の結果と、そのバッチの実行統計。

    **``+`` で畳める。** 段ごとの統計（結果は空）も、並行して走らせたバッチの
    結果も、同じ演算子でまとめられる。結果は受け取った順につながる。
    """

    results: tuple[GeocodeResult, ...] = ()
    usage: Usage = Usage()
    #: Jev を呼ばずに町字が決まった件数
    machiaza_fast_path: int = 0
    #: Jev を呼ばずに番号が決まった件数
    banchi_fast_path: int = 0
    #: 分割絞り込みのために増えた往復数
    narrow_requests: int = 0

    def __add__(self, other: BatchOutcome) -> BatchOutcome:
        return BatchOutcome(
            results=self.results + other.results,
            usage=self.usage + other.usage,
            machiaza_fast_path=self.machiaza_fast_path + other.machiaza_fast_path,
            banchi_fast_path=self.banchi_fast_path + other.banchi_fast_path,
            narrow_requests=self.narrow_requests + other.narrow_requests,
        )
