"""町字より後ろの数値列の取り出し。

**どこまでが住所かをここで決めない**のが仕様。建物名が残ることを確認する。
"""

import pytest

from jev_abr_geocoder.match.banchi_tail import parse_banchi_tail


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
    assert parse_banchi_tail(raw).numbers == numbers


def test_building_name_stops_the_number_run() -> None:
    tail = parse_banchi_tail("1-2-3サンハイツ301")
    assert tail.numbers == (1, 2, 3)  # 301 は拾わない
    assert tail.raw == "1-2-3サンハイツ301"  # 判断材料として丸ごと残す


def test_at_most_three_numbers() -> None:
    assert parse_banchi_tail("1-2-3-4-5").numbers == (1, 2, 3)


def test_first_is_the_narrowing_key() -> None:
    assert parse_banchi_tail("12-3").first == 12
    assert parse_banchi_tail("建物だけ").first is None


def test_number_not_at_the_start_is_found() -> None:
    """ABR に無い小字が残っていても、番地は拾う。

    「福定町字灘屋敷179」の「字灘屋敷」は ABR の町字マスターに無いが、
    地番 179 は福定町の machiaza_id にぶら下がっている。読み飛ばせば引ける。
    """
    tail = parse_banchi_tail("字灘屋敷179")
    assert tail.numbers == (179,)
    assert tail.skipped == "字灘屋敷"


def test_skipped_is_empty_for_a_clean_tail() -> None:
    assert parse_banchi_tail("1-2-3").skipped == ""
    assert parse_banchi_tail("1番2号").skipped == ""


def test_skipped_records_a_building_name_before_a_number() -> None:
    """建物名しか無い場合も数字を拾うが、読み飛ばしたことは残る。"""
    tail = parse_banchi_tail("〇〇ハイツ301")
    assert tail.numbers == (301,)
    assert tail.skipped == "〇〇ハイツ"
