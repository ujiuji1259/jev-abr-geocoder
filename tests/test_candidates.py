"""層1 の候補生成。

**測るべきは再現率**（正解が候補集合に入っているか）であって、1 位かどうかでは
ない。順位付けは Jev の仕事なので、ここで 1 位を固定するテストは書かない。
"""

from jev_abr_geocoder.config import GeocoderConfig
from jev_abr_geocoder.index.townindex import TownIndex
from jev_abr_geocoder.match.candidates import CandidateFinder, prefix_distance
from jev_abr_geocoder.models import Level
from jev_abr_geocoder.textnorm import normalize


def _finder(index: TownIndex) -> CandidateFinder:
    return CandidateFinder(index, GeocoderConfig())


def _displays(index: TownIndex, normalized: str) -> list[str]:
    found = _finder(index).find(normalized)
    records = index.store.towns([c.town_id for c in found.candidates])
    return [records[c.town_id].display for c in found.candidates]


def test_prefix_distance_allows_the_text_to_continue() -> None:
    # 末尾に番地が続いていても、前方一致としては距離 0。
    assert prefix_distance("鳥取県鳥取市面影一丁目", "鳥取県鳥取市面影一丁目1-2", 2) == 0
    assert prefix_distance("面影", "面かげ", 2) == 1
    assert prefix_distance("まったく違う", "鳥取県", 2) == 3  # 打ち切り値


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


def test_typo_falls_back_and_still_recalls_the_answer(index: TownIndex) -> None:
    """前方一致が取れない誤字でも、正解が候補に残ること。

    1 位である必要はない。絞り込むのがトライ、選ぶのが Jev。
    """
    found = _finder(index).find(normalize("鳥取県鳥取市面かげ1丁目1-2"))
    assert not found.exact
    records = index.store.towns([c.town_id for c in found.candidates])
    displays = [records[c.town_id].display for c in found.candidates]
    assert "鳥取県鳥取市面影一丁目" in displays


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


def test_unambiguous_gives_up_on_fuzzy_matches(index: TownIndex) -> None:
    found = _finder(index).find(normalize("鳥取県鳥取市面かげ1丁目1-2"))
    assert not found.exact
    assert found.unambiguous() is None


def test_kanji_chome_matches_arabic_source(index: TownIndex) -> None:
    """ABR が「２丁目」で持っていても、入力の「二丁目」で引けること。"""
    assert "東京都目黒区自由が丘２丁目" in _displays(
        index, normalize("東京都目黒区自由が丘二丁目17-6")
    )
