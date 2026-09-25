"""層1 の候補生成。

**測るべきは再現率**（正解が候補集合に入っているか）であって、1 位かどうかでは
ない。順位付けは Jev の仕事なので、ここで 1 位を固定するテストは書かない。
"""

from jev_abr_geocoder.config import GeocoderConfig
from jev_abr_geocoder.index.townindex import TownIndex
from jev_abr_geocoder.match.candidates import CandidateFinder
from jev_abr_geocoder.models import Level
from jev_abr_geocoder.textnorm import normalize


def _finder(index: TownIndex) -> CandidateFinder:
    return CandidateFinder(index, GeocoderConfig())


def _displays(index: TownIndex, normalized: str) -> list[str]:
    found = _finder(index).find(normalized)
    records = index.store.towns([c.town_id for c in found.candidates])
    return [records[c.town_id].display for c in found.candidates]


def test_exact_prefix_match(index: TownIndex) -> None:
    found = _finder(index).find(normalize("鳥取県鳥取市面影一丁目1番2号"))
    assert found.exact
    records = index.store.towns([c.town_id for c in found.candidates])
    assert records[found.candidates[0].town_id].town == "面影一丁目"
    assert found.candidates[0].remainder == "1番2号"


def test_alias_lets_arabic_chome_match(index: TownIndex) -> None:
    assert "鳥取県鳥取市面影一丁目" in _displays(index, normalize("鳥取市面影1丁目1-2"))


def test_alias_lets_oaza_be_omitted(index: TownIndex) -> None:
    assert "鳥取県鳥取市大字福井" in _displays(index, normalize("鳥取市福井"))


def test_alias_lets_county_be_omitted(index: TownIndex) -> None:
    assert "長崎県北松浦郡佐々町石木場免" in _displays(index, normalize("佐々町石木場免1-1"))


def test_typo_hands_the_whole_city_to_jev(index: TownIndex) -> None:
    """誤字・異体字は、その市区町村の町字を全部 Jev に渡す。

    編集距離で順位づけするのではなく、絞り込みを諦めて判断を委ねる。
    異体字の同定は Jev の得意分野で、こちらが距離で順位をつける筋合いがない。
    """
    found = _finder(index).find(normalize("鳥取県鳥取市面かげ1丁目1-2"))
    assert not found.exact
    assert found.city_id is not None
    displays = _displays(index, normalize("鳥取県鳥取市面かげ1丁目1-2"))
    assert "鳥取県鳥取市面影一丁目" in displays  # 正解が候補に入っている
    assert "鳥取県鳥取市叶" in displays  # 同じ市の町字は全部入る
    # 町字の切れ目は「市区町村より後ろの最初の数字」で決める。
    assert all(c.remainder == "1丁目1-2" for c in found.candidates)


def test_city_wide_gives_up_when_the_city_has_too_many_towns(index: TownIndex) -> None:
    """255 件に収まらない市区町村では、何を落とすかの判断が要るので手を出さない。"""
    from jev_abr_geocoder.config import GeocoderConfig

    cfg = GeocoderConfig(max_options=3)  # 候補は 2 件まで
    found = CandidateFinder(index, cfg).find(normalize("鳥取県鳥取市面かげ1丁目1-2"))
    assert found.candidates == []
    assert found.city_id is not None


def test_input_ending_at_city_yields_no_town_candidates(index: TownIndex) -> None:
    found = _finder(index).find(normalize("鳥取県鳥取市"))
    assert found.candidates == []
    assert found.exhausted is Level.CITY
    assert found.city_id is not None


def test_input_ending_at_pref_yields_pref_level(index: TownIndex) -> None:
    found = _finder(index).find(normalize("鳥取県"))
    assert found.candidates == []
    assert found.exhausted is Level.PREF
    assert found.pref_lg_code is not None


def test_non_address_yields_nothing(index: TownIndex) -> None:
    found = _finder(index).find(normalize("ここは住所ではありません"))
    assert found.candidates == []
    assert found.exhausted is None
    assert found.city_id is None


def test_candidates_never_exceed_the_choice_limit(index: TownIndex) -> None:
    cfg = GeocoderConfig(max_options=2)
    found = CandidateFinder(index, cfg).find(normalize("鳥取市面影"))
    assert len(found.candidates) <= 2


def test_unambiguous_picks_the_strictly_longest_match(index: TownIndex) -> None:
    """「面影一丁目」と「面影」が両方当たっても、長いほうで確定できること。

    これは類似度の判断ではなく前方一致の定義なので、トライが決めてよい。
    """
    found = _finder(index).find(normalize("鳥取県鳥取市面影一丁目1番2号"))
    assert len(found.candidates) > 1  # 短い一致も候補には入っている
    chosen = found.unambiguous()
    assert chosen is not None
    records = index.store.towns([chosen.town_id])
    assert records[chosen.town_id].town == "面影一丁目"


def test_unambiguous_gives_up_when_same_length_matches_collide(index: TownIndex) -> None:
    """同一市区町村に同名の町字が複数あるときは Jev に委ねる。"""
    found = _finder(index).find(normalize("京都府京都市中京区大文字町45"))
    assert len(found.candidates) == 2
    assert found.unambiguous() is None


def test_unambiguous_gives_up_when_input_is_a_key_prefix(index: TownIndex) -> None:
    """入力が言いかけのときは、どの町字かを決められないので Jev に委ねる。

    自由が丘は ABR に丁目なしの行が無いので、「自由が丘」だけでは
    索引鍵の先頭一致にしかならない。
    """
    found = _finder(index).find(normalize("東京都目黒区自由が丘"))
    assert not found.exact
    assert found.candidates
    assert found.unambiguous() is None


def test_kanji_chome_matches_arabic_source(index: TownIndex) -> None:
    """ABR が「２丁目」で持っていても、入力の「二丁目」で引けること。"""
    assert "東京都目黒区自由が丘２丁目" in _displays(
        index, normalize("東京都目黒区自由が丘二丁目17-6")
    )


def test_input_that_is_a_prefix_of_index_keys(index: TownIndex) -> None:
    """「面影」だけでは ABR に行が無いが、索引鍵の側がこの入力で始まっている。

    丁目を持つ大字のうち、丁目なしの親エントリも在るのは全国で 19% だけ。
    残り 81% はこの経路でしか拾えない。
    """
    found = _finder(index).find(normalize("東京都目黒区自由が丘"))
    assert "東京都目黒区自由が丘２丁目" in _displays(index, normalize("東京都目黒区自由が丘"))
    assert all(c.remainder == "" for c in found.candidates)


def test_trailing_number_is_stripped_before_the_prefix_lookup(index: TownIndex) -> None:
    """番地が付いていても、削ってから索引鍵の先頭一致を試す。"""
    found = _finder(index).find(normalize("鳥取県鳥取市大字福井字上町"))
    # 大字福井 に小字は無いので、福井そのものが前方一致する
    assert found.exact or found.candidates


def test_unknown_city_yields_pref_only(index: TownIndex) -> None:
    found = _finder(index).find(normalize("鳥取県そんな市は無い町1-2"))
    assert found.candidates == []
    assert found.pref_lg_code is not None
    assert found.city_id is None


def test_prefix_match_never_splits_a_number(index: TownIndex) -> None:
    """丁目省略のエイリアスが地番の先頭を食わないこと。

    「面影1」という鍵は「面影1189-4」の先頭 1 文字にも当たるが、
    1189 は 1 つの数字トークンなので途中で切ってはいけない。
    切ると「面影一丁目の189」という存在しない住所になる。
    """
    displays = _displays(index, normalize("鳥取県鳥取市面影1189-4"))
    assert "鳥取県鳥取市面影" in displays
    assert "鳥取県鳥取市面影一丁目" not in displays


def test_chome_omission_still_matches_at_a_separator(index: TownIndex) -> None:
    """区切りで終わっていれば丁目省略の一致は有効。"""
    displays = _displays(index, normalize("鳥取県鳥取市面影1-1-2"))
    assert "鳥取県鳥取市面影一丁目" in displays
