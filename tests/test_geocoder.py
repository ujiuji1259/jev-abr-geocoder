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
    model = FakeModel(picks={0: None})
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


async def test_options_leave_room_for_the_none_option(data_dir: Path, model: FakeModel) -> None:
    """判定モデルに渡す候補は max_candidates 件を超えてはならない。

    アダプタが「該当なし」を 1 枠足すので、候補が max_options 件だと Choice が
    256 件になって API が 400 を返し、**バッチ全体が失敗する**。この off-by-one を
    実際に踏んだので、ポートの入口で固定する。アダプタ側の上限は
    tests/test_jev.py で押さえている。
    """
    cfg = GeocoderConfig()
    with _geocoder(data_dir, model, always_rerank=True) as geocoder:
        await geocoder.geocode("東京都目黒区自由が丘")
    assert model.calls, "判定モデルが呼ばれていない"
    for questions in model.calls:
        for question in questions:
            assert len(question.options) <= cfg.max_candidates


def test_max_candidates_leaves_room_for_the_none_option() -> None:
    from jev_abr_geocoder.config import GeocoderConfig

    cfg = GeocoderConfig()
    assert cfg.max_candidates == cfg.max_options - 1


async def test_beam_narrows_a_big_city_in_one_extra_round(data_dir: Path, model: FakeModel) -> None:
    """候補が Choice の上限を超えたら、分割して 1 往復で絞る。

    分割数がいくつでも beam の往復は 1 回。福井市の 15,399 件 (61 分割) でも
    1 リクエストに収まる。
    """
    cfg = GeocoderConfig(max_options=3, beam=True)  # 候補 2 件ごとに分割
    with Geocoder.open(data_dir, model=model, cfg=cfg) as geocoder:
        outcome = await geocoder.run(["鳥取県鳥取市面かげ1丁目1-2"])
    assert outcome.beam_requests == 1
    # beam 1 回 + 町字 1 回。番号は町字が決まってから。
    assert model.request_count <= 3
    for questions in model.calls:
        for question in questions:
            assert len(question.options) <= cfg.max_candidates


async def test_beam_disabled_falls_back_to_city(data_dir: Path, model: FakeModel) -> None:
    cfg = GeocoderConfig(max_options=3, beam=False)
    with Geocoder.open(data_dir, model=model, cfg=cfg) as geocoder:
        result = await geocoder.geocode("鳥取県鳥取市面かげ1丁目1-2")
    assert result.level <= Level.CITY
    assert model.request_count == 0


async def test_parent_parcel_number_resolves_without_jev(data_dir: Path, model: FakeModel) -> None:
    """親番だけ入力され、ABR には枝番付きしか無い場合。

    「石木場免1番地」に対し ABR は 1-1 / 1-2 しか持たない。番号の並びとしては
    入力どおりで枝番が分からないだけなので、どれを選ぶかを Jev に訊いても
    答えようがない。親番で確定する。層1 の「大字はあるが丁目付きしか無い」と
    同じ構造。実データ（鳥取県の法人 20,235 件）では、これで番号到達率が
    88.8% から 95.1% に上がった。
    """
    with _geocoder(data_dir, model) as geocoder:
        result = await geocoder.geocode("長崎県北松浦郡佐々町石木場免1番地")
    assert model.request_count == 0
    assert result.level is Level.PARCEL
    assert result.number == "1番地"
    assert result.prc_id == ""  # 枝番が分からないので ABR の ID は付けない
    assert "枝番" in result.note
    assert result.lat is not None


async def test_block_only_input_resolves_at_block(data_dir: Path, model: FakeModel) -> None:
    """住居表示で街区しか与えられていないときは街区として返す。"""
    with _geocoder(data_dir, model) as geocoder:
        result = await geocoder.geocode("鳥取県鳥取市面影一丁目1番")
    assert model.request_count == 0
    assert result.level is Level.BLOCK
    assert result.number == "1番"


async def test_exact_number_still_wins(data_dir: Path, model: FakeModel) -> None:
    """完全一致があるときは親番に落とさない。"""
    with _geocoder(data_dir, model) as geocoder:
        result = await geocoder.geocode("長崎県北松浦郡佐々町石木場免1-2")
    assert result.level is Level.PARCEL
    assert result.prc_id == "000010000200000"
    assert "枝番" not in result.note


async def test_many_queries_are_split_into_batches(data_dir: Path, model: FakeModel) -> None:
    """呼び出し側が区切らなくても、batch_size ごとに分かれること。

    区切らずに全部を 1 リクエストに入れると Choice の 255 件制限や
    64k tokens/リクエストに当たる。
    """
    queries = ["鳥取県鳥取市面影1丁目1-2"] * 10
    with _geocoder(data_dir, model, batch_size=4, concurrency=2, always_rerank=True) as geocoder:
        results = await geocoder.geocode_many(queries)
    assert len(results) == 10
    # 10 件を 4 件ずつ = 3 バッチ。各バッチが町字と番号で最大 2 往復。
    assert model.request_count <= 6
    for questions in model.calls:
        assert len(questions) <= 4


async def test_results_keep_the_input_order(data_dir: Path) -> None:
    """並行に処理しても、返る順は入力順のまま。"""
    queries = [
        "鳥取県鳥取市面影一丁目1番2号",
        "長崎県北松浦郡佐々町石木場免1-2",
        "鳥取県",
        "東京都目黒区自由が丘二丁目17-6",
        "ここは住所ではありません",
    ] * 3
    with _geocoder(data_dir, None, batch_size=2, concurrency=3) as geocoder:
        results = await geocoder.geocode_many(queries)
    assert [r.query for r in results] == queries


async def test_batch_statistics_are_merged(data_dir: Path, model: FakeModel) -> None:
    with _geocoder(data_dir, model, batch_size=2, concurrency=2) as geocoder:
        outcome = await geocoder.run_all(["鳥取県鳥取市面影一丁目1番2号"] * 6)
    assert len(outcome.results) == 6
    assert outcome.town_fast_path == 6
    assert outcome.number_fast_path == 6


async def test_split_parcels_across_folded_machiaza_are_both_reachable(
    data_dir: Path,
) -> None:
    """ABR が同じ場所を 2 レコードに分けたとき、地番も両方から引ける。

    「字青野」と「青野」のように大字・字の有無だけが違う 2 レコードは同じ
    住所だが、**地番はどちらか一方にしか無い**ことも、**両方に割れている**
    ことも実測されている（栗原市築館新田は字あり 324 筆 / 字なし 10 筆）。
    畳んだ側の machiaza_id も引かないと取りこぼす。
    """
    with Geocoder.open(data_dir, model=None) as geocoder:
        near = await geocoder.geocode("長崎県北松浦郡佐々町字小浦免7番地")
        far = await geocoder.geocode("長崎県北松浦郡佐々町字小浦免120番地")

    assert near.level is Level.PARCEL
    assert near.lat is not None and round(near.lat, 5) == 33.21005

    # 120 番地は畳んだ側 (machiaza_id=2500) にしか無い。
    assert far.level is Level.PARCEL
    assert far.lat is not None and round(far.lat, 5) == 33.21120
