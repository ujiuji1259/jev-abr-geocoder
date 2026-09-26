"""出力の値型。

``geocode_many`` が返すもの。:class:`GeocodeResult` は 1 件分、
:class:`BatchOutcome` はそれとバッチ全体の実行統計。段を追って埋まるので
**この 2 つだけは可変**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .address import Granularity, Point
from .decision import Usage

__all__ = ["GeocodeResult", "BatchOutcome"]


@dataclass(slots=True)
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
    confidence: float = 0.0
    probability: float = 0.0
    note: str = ""

    @property
    def address(self) -> str:
        return f"{self.pref}{self.county}{self.city}{self.ward}{self.machiaza}{self.banchi}"

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
    machiaza_fast_path: int = 0
    #: Jev を呼ばずに番号が決まった件数
    banchi_fast_path: int = 0
    #: 分割絞り込み のために増えた往復数
    narrow_requests: int = 0

    def merge(self, other: BatchOutcome) -> None:
        """バッチの結果を後ろに繋ぐ。``run_all`` が並行実行の結果を畳む。"""
        self.results.extend(other.results)
        self.machiaza_fast_path += other.machiaza_fast_path
        self.banchi_fast_path += other.banchi_fast_path
        self.narrow_requests += other.narrow_requests
        self.usage.merge(other.usage)
