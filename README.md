# jev-abr-geocoder

日本の住所文字列を [アドレス・ベース・レジストリ (ABR)](https://www.digital.go.jp/policies/base_registry_address) に基づいて正規化し、緯度経度を返す。地番まで解決する。

ABR をトライ木として持って前方一致で候補を絞り、候補を [Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) (TypeSafe の System One モデル) の Choice に渡して最も近い住所を選ばせる。

## 使い方

```bash
uv sync

# 索引を作る。machiaza は全国 727,405 町字で約 40 秒・85MB。
uv run jev-abr-geocoder build --level machiaza
uv run jev-abr-geocoder build --level parcel --pref 31   # 地番まで、都道府県を絞って

export TYPESAFE_API_KEY=...        # 未設定なら Jev を使わずトライだけで動く
uv run jev-abr-geocoder normalize "鳥取県鳥取市面影一丁目1番2号"
uv run jev-abr-geocoder normalize --jsonl < addresses.txt > out.jsonl
uv run jev-abr-geocoder info
```

```python
from pathlib import Path
from jev_abr_geocoder import Geocoder

with Geocoder.open(Path("data")) as geocoder:
    results = await geocoder.geocode_many(addresses)
```

`geocode_many` は入力が何件でも Jev の往復を高々 2 回に抑える。

## 実測（geolonia の難例 7,191 件）

| | |
|---|---|
| ファストパス（Jev 不要） | **99.1%** |
| Jev に回る | 0.9%（同名町・異体字・ケ/ヶ の揺れ） |
| 候補生成 | 17 µs/件（7,191件を 1.06 秒） |
| 索引 | `town.marisa` 24MB + `abr.db` 61MB（町字まで） |

## ドキュメント

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | データ構成と容量・性能の実測に基づく設計判断 |
| [docs/code-design.md](docs/code-design.md) | モジュール分割・型・インターフェース |
| [docs/eval.md](docs/eval.md) | 評価セットと指標、閾値の決め方 |

## 状態

層1（町字）と層2（街区・住居番号・地番）、CLI、テストまで実装済み。
Jev を実際に叩いた精度評価はこれから（[docs/eval.md](docs/eval.md) の評価セットが要る）。
