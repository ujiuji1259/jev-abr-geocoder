"""ABR を trie で前方一致させ、候補を Jev に選ばせる住所正規化・ジオコーダ。

    from jev_abr_geocoder import Geocoder

    with Geocoder.open(Path("data")) as geocoder:
        results = await geocoder.geocode_many(addresses)

``geocode_many`` は入力が何件でも Jev の往復を高々 2 回に抑える。1 件だけの
場合も ``geocode`` から同じ経路を通る。

外部依存（Jev・トライ・永続化・HTTP）を差し替えるなら :mod:`ports` の Protocol
を実装し、:meth:`Geocoder.open` の代わりに :class:`Geocoder` を直に組む。既定の
実装は :mod:`jev_abr_geocoder.adapters`。
"""

from . import ports
from ._version import __version__
from .config import GeocoderConfig
from .geocoder import Geocoder
from .models import Decision, GeocodeResult, Level, NumberKind, Point

__all__ = [
    "__version__",
    "Geocoder",
    "GeocoderConfig",
    "GeocodeResult",
    "Decision",
    "Level",
    "NumberKind",
    "Point",
    "ports",
]
