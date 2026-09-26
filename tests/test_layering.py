"""依存の向き。

制約4（外部ライブラリはアダプタの中にしか無い）は設計意図なので、**退行したら
落ちるようにする**。制約1 を `FakeModel` の呼び出し回数で守っているのと同じ扱い。

破れるのは静かなので効く。`geocoder.py` で `import sqlite3` と書いても動いて
しまい、次にバックエンドを触る人が気づくのはそのときである。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import jev_abr_geocoder

_SRC = Path(jev_abr_geocoder.__file__).parent
_ADAPTERS = _SRC / "adapters"

#: コアが import してはならない外部ライブラリ。
_VENDOR = {"sqlite3", "marisa_trie", "httpx", "typesafe_sdk"}

#: ``cli.py`` は実行の入口そのもので、コアではなく最も外側のアダプタ。
#: ここで実装を選ぶのが仕事なので、先頭 import を許す。
_ENTRY_POINTS = {"cli.py"}


def _core_modules() -> list[Path]:
    return sorted(p for p in _SRC.rglob("*.py") if _ADAPTERS not in p.parents)


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", _core_modules(), ids=lambda p: p.name)
def test_core_does_not_import_vendor_libraries(path: Path) -> None:
    leaked = _imported_roots(path) & _VENDOR
    assert not leaked, f"{path.name} が {sorted(leaked)} を直接 import している"


def test_core_module_bodies_do_not_import_adapters() -> None:
    """アダプタを選ぶのは合成の根（関数の中）だけ。

    モジュール先頭で import すると、どのモジュールがどの実装に縛られているかが
    追えなくなる。関数内 import なら grep 一発で合成の根が並ぶ。
    """
    offenders: list[str] = []
    for path in _core_modules():
        if path.name in _ENTRY_POINTS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and "adapters" in {
                alias.name for alias in node.names
            }:
                offenders.append(path.name)
    assert not offenders, f"モジュール先頭で adapters を import している: {offenders}"
