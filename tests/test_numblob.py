"""層2 のバイナリ形式。SQLite を介さない純粋な往復テスト。"""

import random

import pytest

from jev_abr_geocoder.index import numblob
from jev_abr_geocoder.models import NumberEntry, Point


def _sample(count: int, *, with_points: bool = True) -> list[NumberEntry]:
    rng = random.Random(1234)
    out: list[NumberEntry] = []
    for i in range(count):
        point = (
            Point(lat=33.25 + rng.random() * 0.02, lon=129.66 + rng.random() * 0.02)
            if with_points
            else None
        )
        out.append(NumberEntry(num1=i // 4 + 1, num2=i % 4 + 1, num3=0, point=point))
    return out


def test_roundtrip_preserves_numbers_and_coordinates() -> None:
    entries = _sample(1000)
    decoded = numblob.decode(numblob.encode(entries))
    assert len(decoded) == len(entries)
    for original, restored in zip(sorted(entries, key=numblob.sort_key), decoded, strict=True):
        assert (restored.num1, restored.num2, restored.num3) == (
            original.num1,
            original.num2,
            original.num3,
        )
        assert original.point is not None and restored.point is not None
        # 固定小数点 1e-7 度なので、往復誤差は 1e-7 未満に収まる。
        assert abs(restored.point.lat - original.point.lat) < 1e-7
        assert abs(restored.point.lon - original.point.lon) < 1e-7


def test_roundtrip_without_coordinates() -> None:
    entries = _sample(10, with_points=False)
    decoded = numblob.decode(numblob.encode(entries))
    assert all(entry.point is None for entry in decoded)
    assert [e.num1 for e in decoded] == [e.num1 for e in sorted(entries, key=numblob.sort_key)]


def test_mixed_missing_coordinates() -> None:
    entries = [
        NumberEntry(1, 1, 0, Point(33.25, 129.66)),
        NumberEntry(1, 2, 0, None),
        NumberEntry(2, 1, 0, Point(33.26, 129.67)),
    ]
    decoded = numblob.decode(numblob.encode(entries))
    assert [e.point is None for e in decoded] == [False, True, False]


def test_partial_decode_matches_full_decode() -> None:
    """目録で二分探索した部分展開が、全件展開の絞り込みと一致すること。"""
    entries = _sample(2000)
    blob = numblob.encode(entries)
    full = numblob.decode(blob)
    for num1 in (1, 7, 123, 500, 9999):
        expected = [e for e in full if e.num1 == num1]
        actual = numblob.decode(blob, num1=num1)
        assert [(e.num1, e.num2, e.num3) for e in actual] == [
            (e.num1, e.num2, e.num3) for e in expected
        ], f"num1={num1}"


def test_partial_decode_spans_chunk_boundary() -> None:
    """同じ num1 が複数チャンクにまたがっても取りこぼさない。"""
    count = numblob.CHUNK_SIZE * 3 + 7
    entries = [NumberEntry(num1=5, num2=i + 1, num3=0, point=None) for i in range(count)]
    blob = numblob.encode(entries)
    assert len(numblob.decode(blob, num1=5)) == count


def test_empty_input_is_rejected() -> None:
    with pytest.raises(ValueError):
        numblob.encode([])


def test_blob_is_much_smaller_than_naive_rows() -> None:
    """パック BLOB が素の行表現よりはっきり小さいこと。

    実測では 102 B/件 -> 7.7 B/件。ここでは回帰検出のため緩い上限だけ置く。
    """
    entries = _sample(5000)
    blob = numblob.encode(entries)
    assert len(blob) / len(entries) < 15.0
