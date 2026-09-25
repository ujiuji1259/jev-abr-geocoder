"""ABR の DCAT フィードから、取り込むファイルの一覧を決める。

フィードは ``https://dataset.address-br.digital.go.jp/api/feed/dcat-us/1.1.json``
で、9,000 件超のデータセットが並ぶ。実体は ``data.address-br.digital.go.jp``
配下の zip 圧縮 CSV。

このモジュールは索引の構造を知らない。URL の形からファイルの種別と範囲を読み取り、
必要なものを選ぶだけ。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

__all__ = [
    "FEED_URL",
    "DATA_HOST",
    "FileRef",
    "Scope",
    "TEXT_KINDS",
    "POS_KINDS",
    "BuildLevel",
    "kinds_for_level",
    "parse_feed",
    "fetch_feed",
    "select",
]

FEED_URL = "https://dataset.address-br.digital.go.jp/api/feed/dcat-us/1.1.json"
DATA_HOST = "data.address-br.digital.go.jp"

_URL_RE = re.compile(
    rf"https://{re.escape(DATA_HOST)}/(?P<kind>[a-z0-9_]+)/"
    r"(?:(?P<scope>pref|city)/)?(?P<name>[A-Za-z0-9_]+)\.csv\.zip$"
)
_CODE_RE = re.compile(r"(?:pref|city)(\d+)$")


class Scope(str, Enum):
    ALL = "all"
    PREF = "pref"
    CITY = "city"


class BuildLevel(str, Enum):
    """構築の深さ。地番まで入れると 1,887 ファイルになるので段階的に選べる。"""

    MACHIAZA = "machiaza"
    RSDT = "rsdt"
    PARCEL = "parcel"


#: 住所文字列を持つマスター。
TEXT_KINDS = ("mt_pref", "mt_city", "mt_town", "mt_rsdtdsp_blk", "mt_rsdtdsp_rsdt", "mt_parcel")

#: 代表点を持つ位置参照拡張。``<text kind>_pos`` という命名規則。
POS_KINDS = tuple(f"{kind}_pos" for kind in TEXT_KINDS)

_LEVEL_KINDS: dict[BuildLevel, tuple[str, ...]] = {
    BuildLevel.MACHIAZA: ("mt_pref", "mt_city", "mt_town"),
    BuildLevel.RSDT: ("mt_pref", "mt_city", "mt_town", "mt_rsdtdsp_blk", "mt_rsdtdsp_rsdt"),
    BuildLevel.PARCEL: (
        "mt_pref",
        "mt_city",
        "mt_town",
        "mt_rsdtdsp_blk",
        "mt_rsdtdsp_rsdt",
        "mt_parcel",
    ),
}


def kinds_for_level(level: BuildLevel) -> tuple[str, ...]:
    """その深さで必要な種別（位置参照拡張を含む）。"""
    text = _LEVEL_KINDS[level]
    return text + tuple(f"{kind}_pos" for kind in text)


@dataclass(frozen=True, slots=True)
class FileRef:
    """取り込み対象の 1 ファイル。"""

    kind: str
    scope: Scope
    #: 都道府県コード (2 桁) または市区町村コード (6 桁)。全国一括なら空。
    code: str
    url: str
    title: str
    modified: str | None

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    @property
    def is_pos(self) -> bool:
        return self.kind.endswith("_pos")

    @property
    def text_kind(self) -> str:
        """位置参照拡張なら対応する本体の種別、本体ならそのまま。"""
        return self.kind[: -len("_pos")] if self.is_pos else self.kind


def parse_feed(payload: dict[str, Any]) -> list[FileRef]:
    """DCAT フィードの JSON を :class:`FileRef` の一覧にする。"""
    out: list[FileRef] = []
    seen: set[str] = set()
    for dataset in payload.get("dataset", []):
        title = str(dataset.get("title", ""))
        modified = dataset.get("modified")
        for dist in dataset.get("distribution", []):
            url = dist.get("accessURL") or dist.get("downloadURL") or ""
            match = _URL_RE.match(url)
            if match is None or url in seen:
                continue
            seen.add(url)
            scope_raw = match.group("scope")
            scope = Scope(scope_raw) if scope_raw else Scope.ALL
            code_match = _CODE_RE.search(match.group("name"))
            out.append(
                FileRef(
                    kind=match.group("kind"),
                    scope=scope,
                    code=code_match.group(1) if code_match else "",
                    url=url,
                    title=title,
                    modified=str(modified) if modified else None,
                )
            )
    return out


async def fetch_feed(client: httpx.AsyncClient) -> list[FileRef]:
    response = await client.get(FEED_URL, timeout=120.0)
    response.raise_for_status()
    return parse_feed(response.json())


def select(
    refs: Iterable[FileRef],
    kinds: Sequence[str],
    *,
    prefs: Sequence[str] | None = None,
    cities: Sequence[str] | None = None,
) -> list[FileRef]:
    """種別ごとに、必要十分なファイル集合を選ぶ。

    同じ内容が全国一括・都道府県別・市区町村別で重複して配布されているので、
    **絞り込みが無ければ最も粒度の粗いものを 1 つだけ選ぶ**。1,946 個の
    ``mt_town`` ファイルではなく ``mt_town_all.csv.zip`` 1 つで済む。

    ``prefs`` / ``cities`` を与えた場合は、その範囲をちょうど覆う最小の
    ファイル集合を選ぶ。
    """
    wanted = set(kinds)
    by_kind: dict[str, list[FileRef]] = {}
    for ref in refs:
        if ref.kind in wanted:
            by_kind.setdefault(ref.kind, []).append(ref)

    pref_filter = set(prefs or ())
    city_filter = set(cities or ())
    # 市区町村コードの先頭 2 桁は都道府県コード。市区町村指定は都道府県指定も含む。
    implied_prefs = pref_filter | {code[:2] for code in city_filter}

    out: list[FileRef] = []
    for kind in kinds:
        group = by_kind.get(kind, [])
        if not group:
            continue
        out.extend(_select_one_kind(group, implied_prefs, city_filter))
    return out


def _select_one_kind(group: Sequence[FileRef], prefs: set[str], cities: set[str]) -> list[FileRef]:
    by_scope: dict[Scope, list[FileRef]] = {}
    for ref in group:
        by_scope.setdefault(ref.scope, []).append(ref)

    if not prefs and not cities:
        if Scope.ALL in by_scope:
            return by_scope[Scope.ALL][:1]
        return by_scope.get(Scope.PREF) or by_scope.get(Scope.CITY) or []

    # 市区町村指定があり、市区町村別ファイルがあるならそれが最小。
    if cities and Scope.CITY in by_scope:
        chosen = [ref for ref in by_scope[Scope.CITY] if ref.code in cities]
        if chosen:
            return chosen

    if Scope.PREF in by_scope:
        chosen = [ref for ref in by_scope[Scope.PREF] if ref.code in prefs]
        if chosen:
            return chosen

    if Scope.CITY in by_scope:
        chosen = [ref for ref in by_scope[Scope.CITY] if ref.code[:2] in prefs]
        if chosen:
            return chosen

    # 絞り込めない種別（全国一括しか無いもの）は全国一括を取る。
    return by_scope.get(Scope.ALL, [])[:1]
