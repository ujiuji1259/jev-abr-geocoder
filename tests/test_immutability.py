"""値は書き換えられない。

制約5（値型はすべて frozen）は設計意図なので、**退行したら落ちるようにする**。
可変な値型が 1 つ混ざると、そこを通る経路だけ「誰がいつ書いたか」を追う必要が
出てきて、読み手のコストが一気に上がる。

可変でよいのは**溜める器**だけ（``MachiazaTable``、SQLite の接続、関数の中の
一時リスト）。器は dataclass にしていないので、「dataclass はすべて frozen」を
機械的に確かめれば境界が保てる。
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

import jev_abr_geocoder
from jev_abr_geocoder.address import Granularity, MachiazaName, Point
from jev_abr_geocoder.decision import Decision, Usage
from jev_abr_geocoder.outcome import BatchOutcome, GeocodeResult

_SRC = Path(jev_abr_geocoder.__file__).parent


def _dataclasses(path: Path) -> list[tuple[str, bool]]:
    """(クラス名, frozen か) の一覧。"""
    out: list[tuple[str, bool]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ClassDef):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            name = call.func if call else decorator
            if not (isinstance(name, ast.Name) and name.id == "dataclass"):
                continue
            frozen = any(
                kw.arg == "frozen" and isinstance(kw.value, ast.Constant) and kw.value.value
                for kw in (call.keywords if call else [])
            )
            out.append((node.name, frozen))
    return out


@pytest.mark.parametrize("path", sorted(_SRC.rglob("*.py")), ids=lambda p: p.name)
def test_every_dataclass_is_frozen(path: Path) -> None:
    mutable = [name for name, frozen in _dataclasses(path) if not frozen]
    assert not mutable, f"{path.name} の {mutable} が frozen ではない"


def test_results_cannot_be_written_to() -> None:
    result = GeocodeResult(query="鳥取県鳥取市面影一丁目")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.granularity = Granularity.MACHIAZA  # type: ignore[misc]


def test_usage_adds_up_without_mutating() -> None:
    first = Usage(input_tokens=100, output_tokens=10, requests=1)
    second = Usage(input_tokens=50, output_tokens=5, requests=1)
    total = first + second

    assert total == Usage(input_tokens=150, output_tokens=15, requests=2)
    assert first == Usage(input_tokens=100, output_tokens=10, requests=1)


def test_batch_outcomes_add_up_without_mutating() -> None:
    """段ごとの統計と、並行して走らせたバッチの結果を同じ演算子で畳む。"""
    stage = BatchOutcome(usage=Usage(requests=1), machiaza_fast_path=2)
    batch = BatchOutcome(results=(GeocodeResult(query="x"),), banchi_fast_path=3)
    total = stage + batch

    assert total.machiaza_fast_path == 2
    assert total.banchi_fast_path == 3
    assert total.usage.requests == 1
    assert len(total.results) == 1
    assert stage.results == ()  # 元は変わらない


def test_names_build_strings_without_holding_them() -> None:
    """表記の組み立ては MachiazaName の仕事。レコードは持ち方を知らない。"""
    name = MachiazaName("鳥取県", "", "鳥取市", "", "面影", "一丁目", "1", "")

    assert name.machiaza == "面影一丁目"
    assert name.display == "鳥取県鳥取市面影一丁目"
    assert name.oaza_display == "鳥取県鳥取市面影"
    assert name.city_text == "鳥取県鳥取市"


def test_points_and_decisions_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Point(35.0, 135.0).lat = 0.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        Decision.fast().index = 1  # type: ignore[misc]
