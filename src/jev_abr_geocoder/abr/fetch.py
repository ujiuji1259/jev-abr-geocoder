"""ABR ファイルのダウンロードとキャッシュ。

``Last-Modified`` を横に置いて条件付き GET を投げるので、再実行時に変わって
いないファイルは 304 で済む。地番まで取ると 1,887 ファイルになるため、
**再開可能であることが要件**。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from .catalog import FileRef

__all__ = ["Downloader", "Downloaded", "ProgressSink"]

#: (完了数, 総数, 直近のファイル名) を受け取る進捗通知。
ProgressSink = Callable[[int, int, str], None]

_USER_AGENT = "jev-abr-geocoder/0.1 (+https://github.com/ujiuji1259/jev-abr-geocoder)"


@dataclass(frozen=True, slots=True)
class Downloaded:
    ref: FileRef
    path: Path
    #: サーバが返した Last-Modified。差分判定に使う。
    last_modified: str | None
    #: キャッシュがそのまま使えた場合 True
    cached: bool


class Downloader:
    def __init__(
        self,
        cache_dir: Path,
        *,
        concurrency: int = 4,
        client: httpx.AsyncClient | None = None,
        timeout: float = 300.0,
    ) -> None:
        self._cache_dir = cache_dir
        self._semaphore = asyncio.Semaphore(concurrency)
        self._client = client
        self._timeout = timeout

    def _target(self, ref: FileRef) -> Path:
        return self._cache_dir / ref.kind / ref.filename

    @staticmethod
    def _sidecar(path: Path) -> Path:
        return path.with_suffix(path.suffix + ".meta.json")

    def cached_last_modified(self, ref: FileRef) -> str | None:
        """キャッシュ済みファイルの Last-Modified。未取得なら None。"""
        path = self._target(ref)
        sidecar = self._sidecar(path)
        if not path.exists() or not sidecar.exists():
            return None
        try:
            return json.loads(sidecar.read_text())["last_modified"]
        except (OSError, ValueError, KeyError):
            return None

    async def download(self, ref: FileRef, client: httpx.AsyncClient) -> Downloaded:
        path = self._target(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        known = self.cached_last_modified(ref)

        headers = {"User-Agent": _USER_AGENT}
        if known and path.exists():
            headers["If-Modified-Since"] = known

        async with self._semaphore:
            response = await client.get(ref.url, headers=headers, timeout=self._timeout)

        if response.status_code == 304 and path.exists():
            return Downloaded(ref=ref, path=path, last_modified=known, cached=True)
        response.raise_for_status()

        last_modified = response.headers.get("Last-Modified")
        # 壊れた途中結果を残さないよう、一時ファイルに書いてから差し替える。
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(response.content)
        tmp.replace(path)
        self._sidecar(path).write_text(
            json.dumps({"last_modified": last_modified, "url": ref.url}, ensure_ascii=False)
        )
        return Downloaded(ref=ref, path=path, last_modified=last_modified, cached=False)

    async def download_all(
        self, refs: Sequence[FileRef], *, progress: ProgressSink | None = None
    ) -> list[Downloaded]:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(follow_redirects=True)
        done = 0
        results: list[Downloaded] = []
        try:
            tasks = [asyncio.create_task(self.download(ref, client)) for ref in refs]
            for coro in asyncio.as_completed(tasks):
                result = await coro
                done += 1
                results.append(result)
                if progress is not None:
                    progress(done, len(refs), result.ref.filename)
        finally:
            if owns_client:
                await client.aclose()
        return results
