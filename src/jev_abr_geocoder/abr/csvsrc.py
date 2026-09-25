"""zip 圧縮 CSV のストリーム読み。

全国版 ``mt_town_all.csv`` は展開後 156 MB あるので、メモリに載せずに 1 行ずつ
流す。ABR の CSV は UTF-8 (BOM 付きのことがある)。
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

__all__ = ["read_rows", "count_rows"]

# 地番マスターの備考欄など、既定の上限を超えるフィールドがあり得る。
csv.field_size_limit(10_000_000)


def read_rows(path: Path) -> Iterator[dict[str, str]]:
    """zip 内の CSV を 1 行ずつ dict で返す。"""
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError(f"zip に CSV が無い: {path}")
        for name in names:
            with archive.open(name) as raw:
                stream = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                yield from csv.DictReader(stream)


def count_rows(path: Path) -> int:
    return sum(1 for _ in read_rows(path))
