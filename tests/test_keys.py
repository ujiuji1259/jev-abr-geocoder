"""索引側のエイリアス生成。

入力側の正規化を薄く保てているのは、表記ゆれの吸収をここに移したから。
生成規則が壊れると前方一致の再現率が黙って落ちるので、表で固定しておく。
"""

from jev_abr_geocoder.index.keys import (
    CityName,
    TownName,
    city_aliases,
    kanji_number,
    pref_variants,
    town_aliases,
)


def test_pref_variants_includes_omission_and_suffix_drop() -> None:
    assert pref_variants("岩手県") == ("", "岩手県", "岩手")
    assert pref_variants("東京都") == ("", "東京都", "東京")
    assert pref_variants("北海道") == ("", "北海道", "北海")
    assert pref_variants("") == ("",)


def test_city_aliases_cover_county_omission() -> None:
    aliases = city_aliases(CityName("長崎県", "北松浦郡", "佐々町", ""))
    assert "長崎県北松浦郡佐々町" in aliases
    assert "長崎県佐々町" in aliases  # 郡を省略
    assert "佐々町" in aliases  # 都道府県も省略
    assert "北松浦郡佐々町" in aliases


def test_town_aliases_cover_chome_forms() -> None:
    aliases = town_aliases(TownName("鳥取県", "", "鳥取市", "", "面影", "一丁目", "1", ""))
    assert "鳥取県鳥取市面影一丁目" in aliases  # 原文
    assert "鳥取県鳥取市面影1丁目" in aliases  # 算用数字
    assert "鳥取県鳥取市面影1" in aliases  # 丁目そのものを省略
    assert "鳥取市面影1丁目" in aliases  # 都道府県を省略
    assert "鳥取鳥取市面影1丁目" in aliases  # 県を落とした都道府県名


def test_town_aliases_cover_oaza_and_koaza_prefixes() -> None:
    aliases = town_aliases(TownName("鳥取県", "", "鳥取市", "", "大字福井", "", "", "字上町"))
    assert "鳥取県鳥取市大字福井字上町" in aliases  # 原文どおり
    assert "鳥取県鳥取市福井上町" in aliases  # 大字・字ともに省略


def test_town_aliases_do_not_mix_prefix_styles() -> None:
    """大字だけ書いて字は省く、という書き方は実在しないので張らない。

    接頭辞を直積の軸にすると、両方に接頭辞がある町字（全国 57,033 件）が
    4 通りに膨らむ。全国で鍵が 411,259 本（7.9%）増えるのに、評価セット
    2 つで候補集合は 1 件も変わらなかった。
    """
    aliases = town_aliases(TownName("鳥取県", "", "鳥取市", "", "大字福井", "", "", "字上町"))
    assert "鳥取県鳥取市福井字上町" not in aliases
    assert "鳥取県鳥取市大字福井上町" not in aliases


def test_oaza_column_may_carry_the_koaza_prefix() -> None:
    """ABR は大字の列にも「字」を入れてくる（全国 21,315 件）。"""
    aliases = town_aliases(TownName("北海道", "虻田郡", "真狩村", "", "字光", "", "", ""))
    assert "北海道虻田郡真狩村字光" in aliases
    assert "北海道虻田郡真狩村光" in aliases


def test_town_aliases_always_contain_full_form() -> None:
    name = TownName("長崎県", "北松浦郡", "佐々町", "", "石木場免", "", "", "")
    assert "長崎県北松浦郡佐々町石木場免" in town_aliases(name)


def test_town_aliases_are_deduplicated() -> None:
    name = TownName("鳥取県", "", "鳥取市", "", "叶", "", "", "")
    aliases = town_aliases(name)
    assert len(aliases) == len(set(aliases))


def test_kanji_number() -> None:
    assert kanji_number(1) == "一"
    assert kanji_number(9) == "九"
    assert kanji_number(10) == "十"
    assert kanji_number(12) == "十二"
    assert kanji_number(20) == "二十"
    assert kanji_number(35) == "三十五"
    assert kanji_number(100) == "百"
    assert kanji_number(105) == "百五"
    assert kanji_number(0) == ""


def test_town_aliases_add_kanji_chome_when_abr_stores_arabic() -> None:
    """ABR の chome は漢数字とは限らない。

    「自由が丘」の丁目は ABR 側が全角算用数字「２丁目」で収録されているため、
    入力の「二丁目」を引けるよう chome_number から漢数字も生成する。
    """
    aliases = town_aliases(TownName("東京都", "", "目黒区", "", "自由が丘", "２丁目", "2", ""))
    assert "東京都目黒区自由が丘2丁目" in aliases
    assert "東京都目黒区自由が丘二丁目" in aliases
    assert "東京都目黒区自由が丘2" in aliases


def test_town_aliases_add_arabic_chome_when_abr_stores_kanji() -> None:
    aliases = town_aliases(TownName("鳥取県", "", "鳥取市", "", "面影", "一丁目", "1", ""))
    assert "鳥取県鳥取市面影一丁目" in aliases
    assert "鳥取県鳥取市面影1丁目" in aliases
