"""層2 のバイナリ形式。

街区・住居番号・地番は全国で 1〜2 億件になる。1 件 1 行で持つと実測 102 B/件
（全国 10〜20 GB）だが、アクセスパターンは常に「ある町字の番号を全部」なので、
**町字単位でまとめて 1 つの BLOB** にすると 7.7 B/件（全国 0.8〜1.5 GB）に落ちる。

削減の内訳（佐々町 36,558 筆での実測）:

1. 1 件 1 行 → 1 町字 1 BLOB   102 → 10.5 B。SQLite の行オーバーヘッドが消える
2. ID 列を持たない              番号列のゼロ詰めなので復元できる
3. 番号を varint + 差分符号化   番号順にソート済みなので差分はほぼ 0 か 1
4. 座標を町字南西端からの差分   1e-7 度 (約 1 cm) の固定小数点 varint
5. zlib 圧縮                    10.5 → 7.7 B

BLOB は :data:`CHUNK_SIZE` 件ずつ独立に圧縮し、先頭に目録を置く。町字まるごとの
展開は実測 median 2.3 ms / max 9 ms かかるが、目録で二分探索して必要なチャンク
だけ展開すれば町字の大きさによらず 0.2 ms 程度に収まる。

このモジュールは SQLite を知らない純粋なエンコーダ／デコーダで、
往復テストが単体で書ける。
"""

from __future__ import annotations

import bisect
import struct
import zlib
from collections.abc import Iterable, Sequence

from ..address import Banchi, Point

__all__ = ["CHUNK_SIZE", "FORMAT_VERSION", "encode", "decode", "sort_key"]

FORMAT_VERSION = 1

#: 1 チャンクあたりのエントリ数。小さいほど部分展開が速く、圧縮率は下がる。
CHUNK_SIZE = 256

#: 座標の固定小数点スケール。1e-7 度は緯度でおよそ 1.1 cm。
COORD_SCALE = 10_000_000

_HEADER = struct.Struct("<Bii")
_NO_COORD = 0


def sort_key(entry: Banchi) -> tuple[int, int, int]:
    """エントリの整列順。目録の二分探索はこの順序を前提にする。"""
    return (entry.num1, entry.num2, entry.num3)


# ------------------------------------------------------------------ varint


def _put_varint(buf: bytearray, value: int) -> None:
    if value < 0:
        raise ValueError(f"varint に負値は書けない: {value}")
    while True:
        byte = value & 0x7F
        value >>= 7
        buf.append(byte | 0x80 if value else byte)
        if not value:
            return


def _get_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


# ----------------------------------------------------------------- encode


def _encode_chunk(entries: Sequence[Banchi], base_lat: int, base_lon: int) -> bytes:
    buf = bytearray()
    prev_num1 = entries[0].num1
    for entry in entries:
        _put_varint(buf, entry.num1 - prev_num1)
        prev_num1 = entry.num1
        _put_varint(buf, entry.num2)
        _put_varint(buf, entry.num3)
        if entry.point is None:
            _put_varint(buf, _NO_COORD)
        else:
            lat = round(entry.point.lat * COORD_SCALE) - base_lat
            lon = round(entry.point.lon * COORD_SCALE) - base_lon
            # 0 は「座標なし」の符号なので、緯度は 1 だけずらして書く。
            _put_varint(buf, lat + 1)
            _put_varint(buf, lon)
    return zlib.compress(bytes(buf), 6)


def encode(entries: Iterable[Banchi]) -> bytes:
    """町字 1 件分のエントリ列を BLOB にする。

    入力は :func:`sort_key` の順に並べ替えてから書く。
    """
    rows = sorted(entries, key=sort_key)
    if not rows:
        raise ValueError("空のエントリ列は符号化できない")

    lats = [r.point.lat for r in rows if r.point is not None]
    lons = [r.point.lon for r in rows if r.point is not None]
    base_lat = round(min(lats) * COORD_SCALE) if lats else 0
    base_lon = round(min(lons) * COORD_SCALE) if lons else 0

    chunks: list[bytes] = []
    firsts: list[int] = []
    for start in range(0, len(rows), CHUNK_SIZE):
        block = rows[start : start + CHUNK_SIZE]
        firsts.append(block[0].num1)
        chunks.append(_encode_chunk(block, base_lat, base_lon))

    out = bytearray(_HEADER.pack(FORMAT_VERSION, base_lat, base_lon))
    _put_varint(out, len(rows))
    _put_varint(out, len(chunks))
    for first, chunk in zip(firsts, chunks, strict=True):
        _put_varint(out, first)
        _put_varint(out, len(chunk))
    for chunk in chunks:
        out += chunk
    return bytes(out)


# ----------------------------------------------------------------- decode


class _Directory:
    __slots__ = ("base_lat", "base_lon", "count", "firsts", "offsets", "lengths", "body")

    def __init__(self, blob: bytes) -> None:
        version, self.base_lat, self.base_lon = _HEADER.unpack_from(blob, 0)
        if version != FORMAT_VERSION:
            raise ValueError(f"未知の BLOB 形式版: {version}")
        pos = _HEADER.size
        self.count, pos = _get_varint(blob, pos)
        n_chunks, pos = _get_varint(blob, pos)
        firsts: list[int] = []
        lengths: list[int] = []
        for _ in range(n_chunks):
            first, pos = _get_varint(blob, pos)
            length, pos = _get_varint(blob, pos)
            firsts.append(first)
            lengths.append(length)
        self.firsts = firsts
        self.lengths = lengths
        offsets: list[int] = []
        cursor = pos
        for length in lengths:
            offsets.append(cursor)
            cursor += length
        self.offsets = offsets
        self.body = blob


def _decode_chunk(
    blob: bytes,
    offset: int,
    length: int,
    first_num1: int,
    base_lat: int,
    base_lon: int,
    want: int | None = None,
) -> list[Banchi]:
    """チャンクを展開する。

    ``want`` を与えると、``num1`` がそれに一致するものだけを組み立てる。
    1 チャンクは 256 件あるのに必要なのはふつう数件なので、**オブジェクトを作る前に
    絞る**のが効く。実測で 8,000 件の処理が 11.0 秒から 4.4 秒になった。
    """
    data = zlib.decompress(blob[offset : offset + length])
    out: list[Banchi] = []
    pos = 0
    num1 = first_num1
    size = len(data)
    get = _get_varint
    while pos < size:
        delta, pos = get(data, pos)
        num1 += delta
        num2, pos = get(data, pos)
        num3, pos = get(data, pos)
        lat_code, pos = get(data, pos)
        lon_code = 0
        if lat_code != _NO_COORD:
            lon_code, pos = get(data, pos)
        if want is not None and num1 != want:
            # 目当ての番号でなければ Banchi も Point も作らない。
            continue
        point: Point | None = None
        if lat_code != _NO_COORD:
            point = Point(
                lat=(base_lat + lat_code - 1) / COORD_SCALE,
                lon=(base_lon + lon_code) / COORD_SCALE,
            )
        out.append(Banchi(num1=num1, num2=num2, num3=num3, point=point))
    return out


def decode(blob: bytes, *, num1: int | None = None) -> list[Banchi]:
    """BLOB を展開する。

    ``num1`` を与えると、目録で二分探索してその番号を含みうるチャンクだけを
    展開し、一致するエントリのみを返す。町字の大きさによらず一定時間。
    """
    directory = _Directory(blob)
    if num1 is None:
        out: list[Banchi] = []
        for i, offset in enumerate(directory.offsets):
            out.extend(
                _decode_chunk(
                    directory.body,
                    offset,
                    directory.lengths[i],
                    directory.firsts[i],
                    directory.base_lat,
                    directory.base_lon,
                )
            )
        return out

    start, end = _chunk_range(directory.firsts, num1)
    matched: list[Banchi] = []
    for i in range(start, end + 1):
        matched.extend(
            _decode_chunk(
                directory.body,
                directory.offsets[i],
                directory.lengths[i],
                directory.firsts[i],
                directory.base_lat,
                directory.base_lon,
                num1,
            )
        )
    return matched


def _chunk_range(firsts: Sequence[int], num1: int) -> tuple[int, int]:
    """``num1`` を含みうるチャンクの範囲 ``(start, end)``（両端を含む）。

    ``firsts`` は非減少で、チャンク ``i`` の要素は ``firsts[i]`` 以上
    ``firsts[i + 1]`` 以下。したがってチャンク ``i`` が ``num1`` を含みうるのは
    ``firsts[i] <= num1`` かつ ``i`` が最後か ``firsts[i + 1] >= num1`` のとき。

    同じ ``num1`` が複数チャンクにまたがる場合（1 つの街区に数百の住居番号が
    ある等）、範囲は複数チャンクになる。
    """
    end = bisect.bisect_right(firsts, num1) - 1
    if end < 0:
        return 0, -1
    start = max(0, bisect.bisect_left(firsts, num1) - 1)
    return start, end
