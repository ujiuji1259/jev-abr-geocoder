"""Geolonia 住所データの取り込み。

ABR だけでは引けない町字があるため、補完源として使う。

ABR には **丁目や小字を持つ大字について、大字そのものの行が無い**ことがある。
「海老名市柏ケ谷」は一丁目〜六丁目しか無く、「川崎市宮前区野川」は野川本町と
野川台しか無い。入力の番地が旧地番のとき、ABR だけでは町字を決められない。

Geolonia 住所データ (v1) は **ABR・国土数値情報の位置参照情報・郵便番号データの
和集合**なので、こうした地名を持っている。実測で

- ABR が丁目/小字つきしか持たない大字   3,882 件
- ABR に大字ごと無いもの                 3,246 件

が補える。なお v2 は ABR から生成されているため同じ穴が空く。補完源としては
**v1 を使う**。

ライセンスは **CC BY 4.0**。帰属表示が要るので :data:`ATTRIBUTION` を索引の
メタデータに残し、``info`` コマンドで表示する。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx

__all__ = ["GeoloniaTown", "DATA_URL", "ATTRIBUTION", "LICENSE", "download", "read_rows"]

DATA_URL = "https://raw.githubusercontent.com/geolonia/japanese-addresses/master/data/latest.csv"

LICENSE = "CC BY 4.0"

#: CC BY 4.0 の帰属表示。索引のメタデータに残し、info で表示する。
ATTRIBUTION = (
    "町字の一部に Geolonia 住所データ (https://geolonia.github.io/japanese-addresses/) "
    "を使用しています。CC BY 4.0 / (c) Geolonia Inc."
)

_FILENAME = "geolonia-latest.csv"

# 「大字町丁目名」に含まれる丁目。ABR は大字と丁目を別列に持つので、
# 突き合わせのときだけ切り離す。
_CHOME_SUFFIX = ("丁目", "丁")


@dataclass(frozen=True, slots=True)
class GeoloniaTown:
    pref: str
    city: str
    #: 大字町丁目名。ABR と違い大字と丁目が 1 列にまとまっている。
    town: str
    #: 小字・通称名
    koaza: str
    lat: float | None
    lon: float | None

    @property
    def has_chome(self) -> bool:
        return self.town.endswith(_CHOME_SUFFIX)

    @property
    def full(self) -> str:
        return f"{self.pref}{self.city}{self.town}{self.koaza}"


def download(cache_dir: Path, *, client: httpx.Client | None = None) -> Path:
    """CSV を取得してキャッシュする。約 52 MB。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / _FILENAME
    owns = client is None
    http = client or httpx.Client(follow_redirects=True, timeout=300.0)
    try:
        response = http.get(DATA_URL)
        response.raise_for_status()
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(response.content)
        tmp.replace(path)
    finally:
        if owns:
            http.close()
    return path


def read_rows(path: Path) -> Iterator[GeoloniaTown]:
    """CSV を 1 行ずつ読む。"""
    with open(path, encoding="utf-8", newline="") as raw:
        yield from _parse(raw)


def _parse(stream: io.TextIOBase) -> Iterator[GeoloniaTown]:
    for row in csv.DictReader(stream):
        town = row.get("大字町丁目名") or ""
        if not town:
            continue
        yield GeoloniaTown(
            pref=row.get("都道府県名") or "",
            city=row.get("市区町村名") or "",
            town=town,
            koaza=row.get("小字・通称名") or "",
            lat=_float(row.get("緯度")),
            lon=_float(row.get("経度")),
        )


def _float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
