"""テスト用の小さな索引と、差し替え可能な判定モデル。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jev_abr_geocoder.index.keys import TownName, town_aliases
from jev_abr_geocoder.index.store import DB_FILENAME, Store
from jev_abr_geocoder.index.townindex import TRIE_FILENAME, TownIndex, build_trie
from jev_abr_geocoder.models import NumberEntry, NumberKind, Point

_SCALE = 10_000_000


@dataclass(frozen=True)
class _Town:
    lg_code: int
    machiaza_id: int
    pref: str
    county: str
    city: str
    ward: str
    oaza_cho: str
    chome: str
    chome_number: str
    koaza: str
    rsdt_addr_flg: int
    lat: float
    lon: float


#: 実データを模した最小構成。それぞれ試したい性質のために置いてある。
#:
#: - 面影一丁目/二丁目 … 前方一致の分岐
#: - 面影（丁目なし）  … 最長一致が一意になるケース（短い一致も同時に当たる）
#: - 叶               … 曖昧一致の競合相手（1 文字で編集距離が小さく出る）
#: - 大字福井         … 「大字」の省略
#: - 自由が丘２丁目    … ABR 側が全角算用数字で収録している丁目
#: - 北松浦郡佐々町    … 郡の省略と地番
#: - 大文字町 x 2     … 同名の町字が同一市区町村に複数ある（Jev 送りになる）
_TOWNS = [
    _Town(
        312011, 55001, "鳥取県", "", "鳥取市", "", "面影", "一丁目", "1", "", 1, 35.4797, 134.2458
    ),
    _Town(
        312011, 55002, "鳥取県", "", "鳥取市", "", "面影", "二丁目", "2", "", 1, 35.4801, 134.2470
    ),
    _Town(312011, 55000, "鳥取県", "", "鳥取市", "", "面影", "", "", "", 0, 35.4799, 134.2464),
    _Town(312011, 12000, "鳥取県", "", "鳥取市", "", "叶", "", "", "", 0, 35.5100, 134.2200),
    _Town(312011, 13000, "鳥取県", "", "鳥取市", "", "大字福井", "", "", "", 0, 35.5200, 134.2300),
    # ABR が全角算用数字で持っている丁目。入力は「二丁目」で来ることがある。
    _Town(
        131105,
        13002,
        "東京都",
        "",
        "目黒区",
        "",
        "自由が丘",
        "２丁目",
        "2",
        "",
        1,
        35.6079,
        139.6690,
    ),
    # 同一の区に同名の町字が 2 つ（京都の通り名由来）。最長一致が競合する。
    _Town(
        261041,
        20000,
        "京都府",
        "",
        "京都市",
        "中京区",
        "大文字町",
        "",
        "",
        "",
        0,
        35.0100,
        135.7600,
    ),
    _Town(
        261041,
        21000,
        "京都府",
        "",
        "京都市",
        "中京区",
        "大文字町",
        "",
        "",
        "",
        0,
        35.0120,
        135.7620,
    ),
    _Town(
        423912,
        1000,
        "長崎県",
        "北松浦郡",
        "佐々町",
        "",
        "石木場免",
        "",
        "",
        "",
        0,
        33.2535,
        129.6629,
    ),
]

_CITIES = [
    (0, 312011, "鳥取県", "", "鳥取市", ""),
    (1, 423912, "長崎県", "北松浦郡", "佐々町", ""),
    (2, 131105, "東京都", "", "目黒区", ""),
    (3, 261041, "京都府", "", "京都市", "中京区"),
]

_PREFS = [
    (312015, "鳥取県", 35.5036, 134.2383),
    (420006, "長崎県", 32.7448, 129.8737),
    (130001, "東京都", 35.6895, 139.6917),
    (260002, "京都府", 35.0212, 135.7556),
]

#: 面影一丁目の住居番号。1番1号 と 1番2号 と 2番1号。
_RSDT = {
    (312011, 55001): [
        NumberEntry(1, 1, 0, Point(35.47970, 134.24580)),
        NumberEntry(1, 2, 0, Point(35.47975, 134.24590)),
        NumberEntry(2, 1, 0, Point(35.47990, 134.24610)),
    ]
}

#: 佐々町石木場免の地番。1-1 と 1-2 と 2-1。
_PARCEL = {
    (423912, 1000): [
        NumberEntry(1, 1, 0, Point(33.25355, 129.66294)),
        NumberEntry(1, 2, 0, Point(33.25322, 129.66262)),
        NumberEntry(2, 1, 0, Point(33.25324, 129.66316)),
    ]
}


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """小さな索引を 1 度だけ組み立てる。"""
    path = tmp_path_factory.mktemp("index")
    with Store.create(path / DB_FILENAME) as store:
        store.replace_prefs(
            [(lg, name, round(lat * _SCALE), round(lon * _SCALE)) for lg, name, lat, lon in _PREFS]
        )
        store.replace_cities(
            [
                (cid, lg, pref, county, city, ward, None, None)
                for cid, lg, pref, county, city, ward in _CITIES
            ]
        )
        rows: list[tuple[Any, ...]] = []
        pairs: list[tuple[str, tuple[int]]] = []
        for town_id, town in enumerate(_TOWNS):
            rows.append(
                (
                    town_id,
                    town.lg_code,
                    town.machiaza_id,
                    town.pref,
                    town.county,
                    town.city,
                    town.ward,
                    town.oaza_cho,
                    town.chome,
                    town.koaza,
                    town.rsdt_addr_flg,
                    round(town.lat * _SCALE),
                    round(town.lon * _SCALE),
                )
            )
            name = TownName(
                town.pref,
                town.county,
                town.city,
                town.ward,
                town.oaza_cho,
                town.chome,
                town.chome_number,
                town.koaza,
            )
            pairs.extend((alias, (town_id,)) for alias in town_aliases(name))
        store.replace_towns(rows)
        for (lg, machiaza), entries in _RSDT.items():
            store.put_numbers(lg, machiaza, NumberKind.RSDT, entries)
        for (lg, machiaza), entries in _PARCEL.items():
            store.put_numbers(lg, machiaza, NumberKind.PARCEL, entries)
        store.set_meta("schema_version", "1")
        store.commit()

    build_trie(pairs).save(str(path / TRIE_FILENAME))
    return path


@pytest.fixture
def index(data_dir: Path) -> TownIndex:
    return TownIndex.open(data_dir)


@dataclass
class FakeModel:
    """Jev の差し替え。

    ``picks`` は質問 ID からオプション ID への写像。与えられなかった質問は
    先頭のオプションを選ぶ。``calls`` に渡されたリクエストを積むので、
    **バッチ全体で 1 回しか呼ばれていないこと**をテストで検証できる。
    """

    picks: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.95
    calls: list[tuple[Any, Mapping[str, Any]]] = field(default_factory=list)
    fail: bool = False

    async def ask(
        self, state: Any, questions: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], tuple[int, int]]:
        self.calls.append((state, questions))
        if self.fail:
            raise RuntimeError("模擬障害")
        answers: dict[str, Any] = {}
        for key, question in questions.items():
            options: Sequence[str] = list(question.criteria.keys())
            chosen = self.picks.get(key, options[0])
            answers[key] = _FakeChoice(chosen, options, self.confidence)
        return answers, (100, 10)

    @property
    def request_count(self) -> int:
        return len(self.calls)


class _FakeChoice:
    type = "choice"

    def __init__(self, choice: str, options: Sequence[str], confidence: float) -> None:
        self.choice = choice
        self.confidence = confidence
        rest = (1.0 - confidence) / max(1, len(options) - 1)
        self.probabilities = {option: rest for option in options}
        self.probabilities[choice] = confidence


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()
