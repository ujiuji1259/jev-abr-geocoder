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

`geocode_many` は何件でも受け取り、`batch_size` ごとに区切って並行に処理する。
1 バッチあたりの Jev 往復は高々 2 回（候補が多すぎる市区町村だけ 3 回）。

外部依存 — Jev (typesafe-sdk)、トライ (marisa-trie)、永続化 (sqlite3)、HTTP (httpx) —
はすべて `ports.py` の Protocol の後ろにあり、それらを import しているのは
`adapters/` の中だけ。差し替えるならポートを満たすものを渡す。

```python
from jev_abr_geocoder import Geocoder, GeocoderConfig, adapters


class MyModel:  # ports.DecisionModel を満たすだけでよい
    async def choose(self, questions): ...


geocoder = Geocoder(adapters.open_index(Path("data")), MyModel(), GeocoderConfig())
```

**処理時間の大半は Jev の応答待ちで、こちらの計算ではない。** 鳥取県の法人
20,235 件では待ちが 103.6 秒に対しローカル処理は 15.0 秒だった。並行化が
そのまま実効速度になる。

| 並行数 | 所要 | 1 件あたり |
|---:|---:|---:|
| 1 | 113.6 秒 | 5.61 ms |
| **4**（既定） | **28.9 秒** | **1.43 ms** |
| 8 | 16.8 秒 | 0.83 ms |

既定を 4 にしてあるのは Jev のレート制限（1,200 リクエスト/分）に余裕を
持たせるため。8 だと 17 リクエスト/秒に達する。

## 実測（geolonia の難例 7,191 件、Jev 実測）

| | |
|---|---|
| geolonia 出力との一致 | **99.71%** |
| 町字以上に到達 | **100.0%**（未到達 1 件は市区町村名の異体字） |
| ファストパス（Jev 不要） | 99.6% |
| Jev 往復 | 25 回（7,191 件全体で） |
| コスト | **$0.0081**（$0.0011 / 1,000 件） |
| 全体の処理時間 | 6.5 秒（0.90 ms/件） |
| 索引 | `machiaza.marisa` 25MB + `abr.db` 67MB（町字まで） |

一致率は「geolonia との合意率」であって正解率ではない（[docs/eval.md](docs/eval.md) §4）。

異体字は Jev が解く。編集距離では届かなかった精度が出ている。

| 入力 | 出力 | confidence |
|---|---|---|
| 新宿区箪笥町 | 簞笥町 | 0.97 |
| 成田市不動ヶ岡 | 不動ケ岡 | 0.96 |
| 新宿区三栄町 | 四谷三栄町 | 0.83 |
| 花巻市12丁目 | 十二丁目 | 0.89 |
| 和歌山市7番町 | 七番丁 | 0.67 |

## 実測（法人番号公表データ 20,235 件、鳥取県、Jev 実測）

**人間が入力した生の住所**での評価。市区町村コードから ABR の `lg_code` を
チェックディジットで復元できるので、**市区町村だけは金ラベル**が手に入る。

| | |
|---|---|
| 市区町村の正解率 | **100.000%**（20,235 / 20,235、金ラベル） |
| 番号まで到達 | **95.74%**（地番 86.34% / 住居番号 9.27% / 街区 0.13%） |
| 町字で止まった | 3.81% |
| ファストパス | 町字 97.4% / 番号 93.9% |
| コスト | **$0.0466**（$0.0023 / 1,000 件） |
| 処理時間 | **30.4 秒**（1.50 ms/件、並行数 4） |

番号層の確信度は、ファストパスを止めて全件 Jev に通すと p10 0.71 / p50 0.95。
閾値 0.30 には十分な余裕がある。

残る 3.81% を個別に当たったところ、**大半は ABR の地番データ側の欠損**だった。
「鳥取市吉方」は 254 筆しか収録がなく num1 は 50〜381 のうち 114 通りしか無い
（欠番 193）。法人番号データの郵便番号を ABR の `post_code` と突き合わせて、
町字の選択自体は正しいことを確認済み。地番は法務省の登記所備付地図由来で、
整備状況が自治体・地区ごとにまちまちなため、こちら側では埋められない。

## データの出典

- [アドレス・ベース・レジストリ](https://www.digital.go.jp/policies/base_registry_address)（デジタル庁）—
  [利用規約](https://www.digital.go.jp/policies/base_registry_address_tos)
- [Geolonia 住所データ](https://geolonia.github.io/japanese-addresses/) — **CC BY 4.0 / (c) Geolonia Inc.**

ABR には丁目や小字を持つ大字について**大字そのものの行が無い**ことがあり
（「海老名市柏ケ谷」は一丁目〜六丁目しか無い）、そのままでは町字を決められない。
Geolonia 住所データは ABR・国土数値情報の位置参照情報・郵便番号データの和集合
なので、これを補完源に使っている。全国で 62,166 件を補い、上表の難例では町字
到達率が 99.9% から 100.0% になった。`--no-geolonia` で切れる。

## ドキュメント

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | データ構成と容量・性能の実測に基づく設計判断 |
| [docs/code-design.md](docs/code-design.md) | モジュール分割・ポートとアダプタ・型 |
| [docs/eval.md](docs/eval.md) | 評価セットと指標、閾値の決め方 |

## 状態

層1（町字）と層2（街区・住居番号・地番）、CLI、テストまで実装済み。
Jev を実際に叩いた精度評価はこれから（[docs/eval.md](docs/eval.md) の評価セットが要る）。
