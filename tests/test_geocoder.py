"""オーケストレーションの統合テスト。

最も重要なのは :func:`test_batch_costs_at_most_two_requests`。
「入力が何件でも Jev の往復は高々 2 回」は設計意図そのものなので、
退行したら落ちるようにしてある。
"""

from pathlib import Path

import pytest

from jev_abr_geocoder.config import GeocoderConfig
from jev_abr_geocoder.geocoder import Geocoder
from jev_abr_geocoder.models import Level

from .conftest import FakeModel


def _geocoder(data_dir: Path, model: FakeModel | None, **kwargs: object) -> Geocoder:
    return Geocoder.open(data_dir, model=model, cfg=GeocoderConfig(**kwargs))  # type: ignore[arg-type]


async def test_batch_costs_at_most_two_requests(data_dir: Path, model: FakeModel) -> None:
    """20 件の入力でも Jev の往復は町字 1 回・番号 1 回だけ。"""
    queries = ["鳥取県鳥取市面影1丁目1-2"] * 10 + ["鳥取市面かげ1丁目1-1"] * 10
    with _geocoder(data_dir, model, always_rerank=True) as geocoder:
        results = await geocoder.geocode_many(queries)
    assert len(results) == 20
    assert model.request_count <= 2


async def test_single_query_uses_the_same_batched_path(data_dir: Path, model: FakeModel) -> None:
    with _geocoder(data_dir, model, always_rerank=True) as geocoder:
        await geocoder.geocode("鳥取県鳥取市面影1丁目1-2")
    assert model.request_count <= 2


async def test_fast_path_skips_jev_entirely(data_dir: Path, model: FakeModel) -> None:
    """候補も番号も一意なら Jev を呼ばない。"""
    with _geocoder(data_dir, model) as geocoder:
        outcome = await geocoder.run(["鳥取県鳥取市面影一丁目1番2号"])
    assert model.request_count == 0
    assert outcome.town_fast_path == 1
    assert outcome.number_fast_path == 1
    result = outcome.results[0]
    assert result.level is Level.RSDT
    assert result.resolved
    assert result.machiaza_id == "0055001"
    assert result.blk_id == "001"
    assert result.rsdt_id == "002"


async def test_always_rerank_forces_jev(data_dir: Path, model: FakeModel) -> None:
    with _geocoder(data_dir, model, always_rerank=True) as geocoder:
        await geocoder.geocode("鳥取県鳥取市面影一丁目1番2号")
    assert model.request_count == 2


async def test_building_name_is_left_as_rest(data_dir: Path) -> None:
    with _geocoder(data_dir, None) as geocoder:
        result = await geocoder.geocode("鳥取県鳥取市面影一丁目1番2号サンハイツ301")
    assert result.rest == "サンハイツ301"
    assert result.level is Level.RSDT


async def test_parcel_town_resolves_to_parcel(data_dir: Path) -> None:
    with _geocoder(data_dir, None) as geocoder:
        result = await geocoder.geocode("長崎県北松浦郡佐々町石木場免1-2")
    assert result.level is Level.PARCEL
    assert result.prc_id == "000010000200000"
    assert result.lat is not None


async def test_city_only_input_is_resolved_at_city(data_dir: Path) -> None:
    with _geocoder(data_dir, None) as geocoder:
        result = await geocoder.geocode("鳥取県鳥取市")
    assert result.level is Level.CITY
    assert result.resolved
    assert result.note == ""


async def test_pref_only_input_is_resolved_at_pref(data_dir: Path) -> None:
    with _geocoder(data_dir, None) as geocoder:
        result = await geocoder.geocode("鳥取県")
    assert result.level is Level.PREF
    assert result.resolved


async def test_non_address_is_unknown(data_dir: Path) -> None:
    with _geocoder(data_dir, None) as geocoder:
        result = await geocoder.geocode("ここは住所ではありません")
    assert result.level is Level.UNKNOWN
    assert not result.resolved


async def test_model_failure_degrades_instead_of_raising(data_dir: Path, model: FakeModel) -> None:
    """Jev が落ちても例外にせず、語彙スコア最上位で返すこと。"""
    model.fail = True
    with _geocoder(data_dir, model, always_rerank=True) as geocoder:
        result = await geocoder.geocode("鳥取県鳥取市面影1丁目1-2")
    assert "応答しなかった" in result.note
    assert result.level >= Level.CITY  # 何かしらは返る


async def test_low_confidence_raises_granularity(data_dir: Path) -> None:
    """確信度が閾値を割ったら、結果を捨てずに 1 段粗い粒度で返す。"""
    model = FakeModel(confidence=0.2)
    with _geocoder(data_dir, model, always_rerank=True) as geocoder:
        result = await geocoder.geocode("鳥取県鳥取市面影1丁目1-2")
    assert result.level is Level.CITY
    assert not result.resolved
    assert "確信度が低い" in result.note


async def test_none_option_rejects_the_candidate_set(data_dir: Path) -> None:
    """Jev が「該当なし」を選んだら町字を採用しない。"""
    from jev_abr_geocoder.config import NONE_OPTION

    model = FakeModel(picks={"q0": NONE_OPTION})
    with _geocoder(data_dir, model, always_rerank=True) as geocoder:
        result = await geocoder.geocode("鳥取県鳥取市面影1丁目1-2")
    assert result.level <= Level.CITY
    assert not result.resolved


async def test_usage_is_reported(data_dir: Path, model: FakeModel) -> None:
    with _geocoder(data_dir, model, always_rerank=True) as geocoder:
        outcome = await geocoder.run(["鳥取県鳥取市面影1丁目1-2"])
    assert outcome.usage.requests == model.request_count
    assert outcome.usage.input_tokens > 0


async def test_empty_batch(data_dir: Path, model: FakeModel) -> None:
    with _geocoder(data_dir, model) as geocoder:
        assert await geocoder.geocode_many([]) == []
    assert model.request_count == 0


@pytest.mark.parametrize(
    "query",
    [
        "鳥取市面影1丁目1-2",  # 都道府県の省略
        "鳥取県鳥取市面影1-1-2",  # 丁目そのものの省略
        "鳥取鳥取市面影一丁目1番2号",  # 「県」を落とした都道府県名
        "鳥取県鳥取市面影１丁目１−２",  # 全角
    ],
)
async def test_alias_variants_reach_the_same_town(data_dir: Path, query: str) -> None:
    with _geocoder(data_dir, None) as geocoder:
        result = await geocoder.geocode(query)
    assert result.machiaza_id == "0055001", result.to_dict()


async def test_city_omission_is_not_an_alias(data_dir: Path) -> None:
    """市区町村の省略は索引しない。曖昧さが大きすぎて候補を絞れなくなるため。"""
    with _geocoder(data_dir, None) as geocoder:
        result = await geocoder.geocode("鳥取面影一丁目1番2号")
    assert result.machiaza_id == ""


async def test_longest_unique_match_skips_jev(data_dir: Path, model: FakeModel) -> None:
    """短い一致が同時に当たっていても、最長一致が一意なら Jev を呼ばない。"""
    with _geocoder(data_dir, model) as geocoder:
        outcome = await geocoder.run(["鳥取県鳥取市面影一丁目1番2号"])
    assert model.request_count == 0
    assert outcome.town_fast_path == 1
    assert outcome.results[0].machiaza_id == "0055001"


async def test_same_name_towns_go_to_jev(data_dir: Path, model: FakeModel) -> None:
    """同名の町字が並ぶときは Jev に判断させる。"""
    with _geocoder(data_dir, model) as geocoder:
        outcome = await geocoder.run(["京都府京都市中京区大文字町45"])
    assert model.request_count >= 1
    assert outcome.town_fast_path == 0
    assert outcome.results[0].level >= Level.MACHIAZA


async def test_kanji_chome_resolves_end_to_end(data_dir: Path) -> None:
    with _geocoder(data_dir, None) as geocoder:
        result = await geocoder.geocode("東京都目黒区自由が丘二丁目17-6")
    assert result.machiaza_id == "0013002"
    assert result.level is Level.MACHIAZA
