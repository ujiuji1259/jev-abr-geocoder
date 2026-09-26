"""Geolonia 住所データの取得と読み。

取得は :class:`ports.HttpClient` 越しなので、ネットワークに出ずに検証できる。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from jev_abr_geocoder import ports
from jev_abr_geocoder.abr import geolonia

_CSV = (
    "都道府県コード,都道府県名,市区町村コード,市区町村名,大字町丁目名,小字・通称名,緯度,経度\n"
    "31,鳥取県,31201,鳥取市,面影一丁目,,35.4797,134.2458\n"
    "31,鳥取県,31201,鳥取市,吉方,新田,,\n"
    "31,鳥取県,31201,鳥取市,,,35.5,134.2\n"  # 大字が空の行は落とす
)


class _FakeClient:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.urls: list[str] = []

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> ports.HttpResponse:
        self.urls.append(url)
        return ports.HttpResponse(status_code=200, content=self._content, url=url)

    async def close(self) -> None: ...


async def test_download_writes_the_csv(tmp_path: Path) -> None:
    client = _FakeClient(_CSV.encode())
    path = await geolonia.download(tmp_path / "cache", client)

    assert client.urls == [geolonia.DATA_URL]
    assert path.exists()
    assert path.read_text(encoding="utf-8") == _CSV


async def test_download_leaves_no_partial_file(tmp_path: Path) -> None:
    """途中結果を残さない。一時ファイルに書いてから差し替える。"""
    path = await geolonia.download(tmp_path / "cache", _FakeClient(_CSV.encode()))
    assert list(path.parent.iterdir()) == [path]


async def test_read_rows_skips_rows_without_a_town(tmp_path: Path) -> None:
    path = await geolonia.download(tmp_path / "cache", _FakeClient(_CSV.encode()))
    rows = list(geolonia.read_rows(path))

    assert [(r.town, r.koaza) for r in rows] == [("面影一丁目", ""), ("吉方", "新田")]
    assert rows[0].lat == pytest.approx(35.4797)
    # 座標を持たない行もある。補完源なので座標が無くても町字としては使う。
    assert rows[1].lat is None
