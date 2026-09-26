"""テスト用の小さな索引と、差し替え可能な判定モデル。

索引は既定のアダプタ（SQLite + marisa-trie）で本物を組む。判定モデルだけは
:class:`ports.DecisionModel` を満たす :class:`FakeModel` を差し込む。Jev の
Choice の作り方も応答の形も知らなくてよいのが、ポートを切ってある効き目。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from jev_abr_geocoder import adapters, ports
from jev_abr_geocoder.address import (
    Banchi,
    BanchiKind,
    CityRecord,
    MachiazaName,
    MachiazaRecord,
    Point,
    PrefRecord,
)
from jev_abr_geocoder.decision import Decision, Usage
from jev_abr_geocoder.index.keys import machiaza_aliases
from jev_abr_geocoder.index.machiaza_index import MachiazaIndex


@dataclass(frozen=True)
class _Machiaza:
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
    #: 同じ場所の別レコードの machiaza_id。
    alt_machiaza: tuple[int, ...] = ()


#: 実データを模した最小構成。それぞれ試したい性質のために置いてある。
#:
#: - 面影一丁目/二丁目 … 前方一致の分岐
#: - 面影（丁目なし）  … 最長一致が一意になるケース（短い一致も同時に当たる）
#: - 叶               … 曖昧一致の競合相手（1 文字で編集距離が小さく出る）
#: - 大字福井         … 「大字」の省略
#: - 自由が丘２丁目    … ABR 側が全角算用数字で収録している丁目
#: - 北松浦郡佐々町    … 郡の省略と地番
#: - 大文字町 x 2     … 同名の町字が同一市区町村に複数ある（Jev 送りになる）
_MACHIAZA = [
    _Machiaza(
        312011, 55001, "鳥取県", "", "鳥取市", "", "面影", "一丁目", "1", "", 1, 35.4797, 134.2458
    ),
    _Machiaza(
        312011, 55002, "鳥取県", "", "鳥取市", "", "面影", "二丁目", "2", "", 1, 35.4801, 134.2470
    ),
    _Machiaza(312011, 55000, "鳥取県", "", "鳥取市", "", "面影", "", "", "", 0, 35.4799, 134.2464),
    _Machiaza(312011, 12000, "鳥取県", "", "鳥取市", "", "叶", "", "", "", 0, 35.5100, 134.2200),
    _Machiaza(
        312011, 13000, "鳥取県", "", "鳥取市", "", "大字福井", "", "", "", 0, 35.5200, 134.2300
    ),
    # ABR が全角算用数字で持っている丁目。入力は「二丁目」で来ることがある。
    _Machiaza(
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
    _Machiaza(
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
    _Machiaza(
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
    _Machiaza(
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
    # ABR が同じ場所を「字○○」と「○○」の 2 レコードに分けて持ち、地番も
    # 両方に割れている型（栗原市築館新田で実測）。代表 1 行に畳み、
    # alt_machiaza にもう一方の machiaza_id を持たせる。
    _Machiaza(
        423912,
        2000,
        "長崎県",
        "北松浦郡",
        "佐々町",
        "",
        "字小浦免",
        "",
        "",
        "",
        0,
        33.2100,
        129.6500,
        alt_machiaza=(2500,),
    ),
]

_CITIES = [
    (312011, "鳥取県", "", "鳥取市", ""),
    (423912, "長崎県", "北松浦郡", "佐々町", ""),
    (131105, "東京都", "", "目黒区", ""),
    (261041, "京都府", "", "京都市", "中京区"),
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
        Banchi(1, 1, 0, Point(35.47970, 134.24580)),
        Banchi(1, 2, 0, Point(35.47975, 134.24590)),
        Banchi(2, 1, 0, Point(35.47990, 134.24610)),
    ]
}

#: 佐々町石木場免の地番。1-1 と 1-2 と 2-1。
_PARCEL = {
    (423912, 1000): [
        Banchi(1, 1, 0, Point(33.25355, 129.66294)),
        Banchi(1, 2, 0, Point(33.25322, 129.66262)),
        Banchi(2, 1, 0, Point(33.25324, 129.66316)),
    ],
    # 字小浦免。代表の machiaza_id に 7 番地、畳んだ側に 120 番地。
    (423912, 2000): [Banchi(7, 0, 0, Point(33.21005, 129.65004))],
    (423912, 2500): [Banchi(120, 0, 0, Point(33.21120, 129.65110))],
}


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """小さな索引を 1 度だけ組み立てる。"""
    path = tmp_path_factory.mktemp("index")
    store = adapters.create_writer(path)
    try:
        store.replace_prefs(
            PrefRecord(lg_code=lg, pref=name, point=Point(lat=lat, lon=lon))
            for lg, name, lat, lon in _PREFS
        )
        store.replace_cities(
            CityRecord(lg_code=lg, pref=pref, county=county, city=city, ward=ward)
            for lg, pref, county, city, ward in _CITIES
        )
        records: list[MachiazaRecord] = []
        pairs: list[tuple[str, int]] = []
        for row_id, machiaza in enumerate(_MACHIAZA):
            name = MachiazaName(
                pref=machiaza.pref,
                county=machiaza.county,
                city=machiaza.city,
                ward=machiaza.ward,
                oaza_cho=machiaza.oaza_cho,
                chome=machiaza.chome,
                chome_number=machiaza.chome_number,
                koaza=machiaza.koaza,
            )
            records.append(
                MachiazaRecord(
                    row_id=row_id,
                    lg_code=machiaza.lg_code,
                    machiaza_id=machiaza.machiaza_id,
                    name=name,
                    rsdt_addr_flg=machiaza.rsdt_addr_flg,
                    point=Point(lat=machiaza.lat, lon=machiaza.lon),
                    alt_machiaza=machiaza.alt_machiaza,
                )
            )
            pairs.extend((alias, row_id) for alias in machiaza_aliases(name))
        store.replace_machiaza(records)
        store.put_banchi(
            [(lg, machiaza, BanchiKind.RSDT, entries) for (lg, machiaza), entries in _RSDT.items()]
            + [
                (lg, machiaza, BanchiKind.PARCEL, entries)
                for (lg, machiaza), entries in _PARCEL.items()
            ]
        )
        store.commit()
    finally:
        store.close()

    adapters.trie_backend().save(pairs, adapters.trie_path(path))
    return path


@pytest.fixture
def index(data_dir: Path) -> MachiazaIndex:
    return adapters.open_index(data_dir)


@dataclass
class FakeModel:
    """判定モデルの差し替え。:class:`ports.DecisionModel` を満たす。

    ``picks`` は問の添字から選ぶ選択肢の添字への写像。``None`` を与えると
    「候補のいずれでもない」を選ぶ。与えられなかった問は先頭を選ぶ。
    ``calls`` に渡された問を積むので、**バッチ全体で 1 回しか呼ばれていない
    こと**をテストで検証できる。
    """

    picks: dict[int, int | None] = field(default_factory=dict)
    confidence: float = 0.95
    calls: list[Sequence[ports.Question]] = field(default_factory=list)
    fail: bool = False

    async def choose(self, questions: Sequence[ports.Question]) -> ports.Answers:
        self.calls.append(list(questions))
        if self.fail:
            raise ports.ModelUnavailable("模擬障害")
        return ports.Answers(
            decisions=tuple(self._decide(i, q) for i, q in enumerate(questions)),
            usage=Usage(input_tokens=100, output_tokens=10, requests=1),
        )

    def _decide(self, index: int, question: ports.Question) -> Decision:
        """「候補のいずれでもない」を含む確率分布を模す。

        選んだものに ``confidence``、残りに均等。実装が閾値の扱いを変えたら
        落ちるよう、確率の出方まで本物に合わせておく。
        """
        rest = (1.0 - self.confidence) / max(1, len(question.options))
        chosen = self.picks.get(index, 0)
        if chosen is None:
            return Decision(
                index=None,
                probability=self.confidence,
                confidence=self.confidence,
                contains_answer=1.0 - self.confidence,
            )
        return Decision(
            index=chosen,
            probability=self.confidence,
            confidence=self.confidence,
            contains_answer=1.0 - rest,
        )

    @property
    def request_count(self) -> int:
        return len(self.calls)


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()
