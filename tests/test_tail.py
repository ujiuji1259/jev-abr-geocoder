"""町字より後ろの数値列の取り出し。

**どこまでが住所かをここで決めない**のが仕様。建物名が残ることを確認する。
"""

import pytest

from jev_abr_geocoder.match.tail import parse_tail


@pytest.mark.parametrize(
    ("raw", "numbers"),
    [
        ("1-2-3", (1, 2, 3)),
        ("1丁目2番3号", (1, 2, 3)),
        ("1番地の2", (1, 2)),
        ("1番2号", (1, 2)),
        ("17番地11", (17, 11)),
        ("", ()),
        ("マンション名のみ", ()),
        ("-1-2", (1, 2)),
    ],
)
def test_numbers(raw: str, numbers: tuple[int, ...]) -> None:
    assert parse_tail(raw).numbers == numbers


def test_building_name_stops_the_number_run() -> None:
    tail = parse_tail("1-2-3サンハイツ301")
    assert tail.numbers == (1, 2, 3)  # 301 は拾わない
    assert tail.raw == "1-2-3サンハイツ301"  # 判断材料として丸ごと残す


def test_at_most_three_numbers() -> None:
    assert parse_tail("1-2-3-4-5").numbers == (1, 2, 3)


def test_first_is_the_narrowing_key() -> None:
    assert parse_tail("12-3").first == 12
    assert parse_tail("建物だけ").first is None
