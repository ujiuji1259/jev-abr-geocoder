"""入力側の正規化は NFKC と空白除去だけ、という約束を守っているかの確認。

**ここで表記ゆれが吸収されないことこそが仕様。** 異体字や漢数字を畳み込む
テストを足したくなったら、それは `index/keys.py` のエイリアスを増やすべき場面。
"""

import pytest

from jev_abr_geocoder.textnorm import normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("鳥取県鳥取市面影１丁目", "鳥取県鳥取市面影1丁目"),  # 全角数字
        ("鳥取県 鳥取市　面影1丁目", "鳥取県鳥取市面影1丁目"),  # 半角・全角空白
        ("ﾄｯﾄﾘ", "トットリ"),  # 半角カナ
        ("１−２", "1−2"),  # 全角ハイフンは NFKC では変わらない
        ("", ""),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_normalize_is_idempotent() -> None:
    once = normalize("鳥取県　鳥取市面影１丁目１−２")
    assert normalize(once) == once


def test_normalize_does_not_fold_variants() -> None:
    """異体字・漢数字はここでは畳まない。索引側のエイリアスが担当する。"""
    assert normalize("菟道") != normalize("莵道")
    assert normalize("一丁目") != normalize("1丁目")
