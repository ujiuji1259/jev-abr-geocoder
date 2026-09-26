"""境界の値型。

:class:`ports.HttpResponse` はアダプタが httpx の応答を平らな dict に直して
作る。httpx のヘッダは大小文字を区別しないので、**平らにした後も
``Last-Modified`` を引けること**が条件付き GET の前提になる。
"""

from __future__ import annotations

import pytest

from jev_abr_geocoder import ports


def test_header_lookup_ignores_case() -> None:
    response = ports.HttpResponse(status_code=200, content=b"", headers={"last-modified": "Mon"})
    assert response.header("Last-Modified") == "Mon"
    assert response.header("If-None-Match") is None


def test_not_modified_is_not_an_error() -> None:
    response = ports.HttpResponse(status_code=304, content=b"")
    assert response.not_modified
    response.raise_for_status()


def test_raise_for_status_carries_the_url() -> None:
    response = ports.HttpResponse(status_code=503, content=b"", url="https://example.invalid/x")
    with pytest.raises(ports.HttpError) as caught:
        response.raise_for_status()
    assert caught.value.status_code == 503
    assert "example.invalid" in str(caught.value)
