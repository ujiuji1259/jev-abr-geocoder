"""``httpx`` を :class:`ports.HttpClient` に合わせる。

**httpx を import していいのはこのファイルだけ。** ``abr/`` は取得の段取り
（条件付き GET・キャッシュ・並行数）だけを持ち、HTTP の実装を知らない。
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from .. import ports

__all__ = ["HttpxClient"]

_USER_AGENT = "jev-abr-geocoder/0.1 (+https://github.com/ujiuji1259/jev-abr-geocoder)"


class HttpxClient:
    """:class:`ports.HttpClient` の httpx 実装。

    リダイレクトを追うのは ABR の配布 URL が 302 を返すため。既定の
    タイムアウトが長いのは、全国一括の zip が 50 MB を超えるため。
    """

    def __init__(self, client: httpx.AsyncClient | None = None, *, timeout: float = 300.0) -> None:
        self._client = client or httpx.AsyncClient(
            follow_redirects=True, headers={"User-Agent": _USER_AGENT}
        )
        self._owns_client = client is None
        self._timeout = timeout

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> ports.HttpResponse:
        response = await self._client.get(
            url,
            headers=dict(headers) if headers else None,
            timeout=self._timeout if timeout is None else timeout,
        )
        return ports.HttpResponse(
            status_code=response.status_code,
            content=response.content,
            headers=dict(response.headers),
            url=url,
        )

    async def close(self) -> None:
        # 外から渡された client は渡した側が閉じる。
        if self._owns_client:
            await self._client.aclose()
