"""DCAT フィードの解釈とファイル選択。

同じ内容が全国一括・都道府県別・市区町村別で重複配布されているので、
**必要最小限のファイルだけを選べているか**が肝心。取りこぼすと索引が欠け、
取りすぎると 1,946 個の町字ファイルを落としにいってしまう。
"""

from jev_abr_geocoder.abr.catalog import BuildLevel, Scope, kinds_for_level, parse_feed, select

_HOST = "https://data.address-br.digital.go.jp"


def _feed(*urls: str) -> dict[str, object]:
    return {
        "dataset": [
            {
                "title": url.rsplit("/", 1)[-1],
                "modified": "2026-09-17T22:35:20.000Z",
                "distribution": [
                    {"accessURL": "https://dataset.address-br.digital.go.jp/documents/x"},
                    {"accessURL": url},
                ],
            }
            for url in urls
        ]
    }


def test_parse_feed_reads_kind_scope_and_code() -> None:
    refs = parse_feed(
        _feed(
            f"{_HOST}/mt_town/mt_town_all.csv.zip",
            f"{_HOST}/mt_town/pref/mt_town_pref31.csv.zip",
            f"{_HOST}/mt_parcel/city/mt_parcel_city423912.csv.zip",
        )
    )
    assert [(r.kind, r.scope, r.code) for r in refs] == [
        ("mt_town", Scope.ALL, ""),
        ("mt_town", Scope.PREF, "31"),
        ("mt_parcel", Scope.CITY, "423912"),
    ]
    assert refs[0].modified is not None


def test_parse_feed_ignores_non_data_urls() -> None:
    refs = parse_feed(
        {"dataset": [{"title": "x", "distribution": [{"accessURL": "https://example.com/a.csv"}]}]}
    )
    assert refs == []


def test_pos_kind_maps_back_to_its_text_kind() -> None:
    refs = parse_feed(_feed(f"{_HOST}/mt_town_pos/pref/mt_town_pos_pref31.csv.zip"))
    assert refs[0].is_pos
    assert refs[0].text_kind == "mt_town"


def test_select_prefers_the_single_nationwide_file() -> None:
    refs = parse_feed(
        _feed(
            f"{_HOST}/mt_town/mt_town_all.csv.zip",
            f"{_HOST}/mt_town/pref/mt_town_pref31.csv.zip",
            f"{_HOST}/mt_town/city/mt_town_city312011.csv.zip",
        )
    )
    chosen = select(refs, ["mt_town"])
    assert [r.scope for r in chosen] == [Scope.ALL]


def test_select_narrows_to_the_requested_pref() -> None:
    refs = parse_feed(
        _feed(
            f"{_HOST}/mt_town/mt_town_all.csv.zip",
            f"{_HOST}/mt_town/pref/mt_town_pref31.csv.zip",
            f"{_HOST}/mt_town/pref/mt_town_pref13.csv.zip",
        )
    )
    chosen = select(refs, ["mt_town"], prefs=["31"])
    assert [r.code for r in chosen] == ["31"]


def test_city_filter_implies_its_pref() -> None:
    """市区町村別ファイルが無い種別でも、その都道府県のファイルで補える。"""
    refs = parse_feed(
        _feed(
            f"{_HOST}/mt_rsdtdsp_rsdt/pref/mt_rsdtdsp_rsdt_pref42.csv.zip",
            f"{_HOST}/mt_rsdtdsp_rsdt/pref/mt_rsdtdsp_rsdt_pref31.csv.zip",
        )
    )
    chosen = select(refs, ["mt_rsdtdsp_rsdt"], cities=["423912"])
    assert [r.code for r in chosen] == ["42"]


def test_select_prefers_city_files_when_available() -> None:
    refs = parse_feed(
        _feed(
            f"{_HOST}/mt_parcel/city/mt_parcel_city423912.csv.zip",
            f"{_HOST}/mt_parcel/city/mt_parcel_city312011.csv.zip",
        )
    )
    chosen = select(refs, ["mt_parcel"], cities=["423912"])
    assert [r.code for r in chosen] == ["423912"]


def test_kinds_for_level_is_cumulative_and_includes_positions() -> None:
    machiaza = set(kinds_for_level(BuildLevel.MACHIAZA))
    parcel = set(kinds_for_level(BuildLevel.PARCEL))
    assert machiaza < parcel
    assert "mt_town" in machiaza and "mt_town_pos" in machiaza
    assert "mt_parcel" in parcel and "mt_parcel_pos" in parcel
    assert "mt_parcel" not in machiaza
