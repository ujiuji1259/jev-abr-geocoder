# jev-abr-geocoder

日本の住所文字列を [アドレス・ベース・レジストリ (ABR)](https://www.digital.go.jp/policies/base_registry_address) に基づいて正規化し、緯度経度を返す。地番まで解決する。

ABR をトライ木として持って前方一致で候補を絞り、候補を [Jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev) (TypeSafe の System One モデル) の Choice に渡して最も近い住所を選ばせる。

## ドキュメント

| | |
|---|---|
| [docs/architecture.md](docs/architecture.md) | データ構成と容量・性能の実測に基づく設計判断 |
| [docs/code-design.md](docs/code-design.md) | モジュール分割・型・インターフェース |
| [docs/eval.md](docs/eval.md) | 評価セットと指標、閾値の決め方 |

## 状態

設計中。実装はまだない。
