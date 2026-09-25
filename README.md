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

## 実測（geolonia の難例 7,191 件、Jev 実測）

| | |
|---|---|
| geolonia 出力との一致 | **99.64%** |
| 町字以上に到達 | **99.9%**（未到達は 5 件） |
| ファストパス（Jev 不要） | 99.5% |
| Jev 往復 | 31 回（7,191 件全体で） |
| コスト | **$0.0093**（$0.0013 / 1,000 件） |
| 全体の処理時間 | 7.3 秒（1.01 ms/件） |
| 索引 | `town.marisa` 24MB + `abr.db` 61MB（町字まで） |

一致率は「geolonia との合意率」であって正解率ではない（[docs/eval.md](docs/eval.md) §4）。

異体字は Jev が解く。編集距離では届かなかった精度が出ている。

| 入力 | 出力 | confidence |
|---|---|---|
| 新宿区箪笥町 | 簞笥町 | 0.97 |
| 成田市不動ヶ岡 | 不動ケ岡 | 0.96 |
| 新宿区三栄町 | 四谷三栄町 | 0.83 |
| 花巻市12丁目 | 十二丁目 | 0.89 |
| 和歌山市7番町 | 七番丁 | 0.67 |

### 残る未到達 5 件

3 件（向日市鶏冠井町・海老名市柏ケ谷・川崎市宮前区野川）は **ABR に
丁目も小字も持たない大字の行が無い**ため、町字としては決められない。
Jev が「該当なし」を返すのは正しい判断。名古屋市緑区鳴海町も同じ型。
残り 1 件は市区町村名そのものの異体字（巿川巿 の「巿」）。

## ドキュメント

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | データ構成と容量・性能の実測に基づく設計判断 |
| [docs/code-design.md](docs/code-design.md) | モジュール分割・型・インターフェース |
| [docs/eval.md](docs/eval.md) | 評価セットと指標、閾値の決め方 |

## 状態

層1（町字）と層2（街区・住居番号・地番）、CLI、テストまで実装済み。
Jev を実際に叩いた精度評価はこれから（[docs/eval.md](docs/eval.md) の評価セットが要る）。
