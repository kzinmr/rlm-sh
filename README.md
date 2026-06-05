# rlm-sh

**Bash & Filesystem 上で動く RLM（Recursive Language Models）プロトタイプ**

RLM が *Python REPL + 変数(メモリ) + `llm_query()` 関数* で長コンテキストを再帰処理するのに対し、
rlm-sh は *bash + ファイルシステム(メモリ) + [`llm`](https://github.com/simonw/llm) CLI* で同じ挙動が成立するかを観察する実験台です。

> *LLM agent on REPL w/ variable & `llm_query`* → *LLM agent on bash w/ filesystem & `llm` CLI*

RLM 著者自身が「Python REPL は一つの具体化に過ぎず、本質は **LLM 呼び出しがコード内で行われ出力がメインモデルの文脈に載らない symbolic environment**」と述べている。rlm-sh はその symbolic environment を **bash + filesystem** で具体化したものです。

- ステータス: 設計フェーズ（[docs/design.md](docs/design.md) Draft **v0.4**）。実装は未着手。

## 中核となる対応（RLM → rlm-sh）

| RLM (Python REPL) | rlm-sh (bash + filesystem) |
|---|---|
| REPL 変数（in-memory） | **ファイル**（`/work/` 配下）＝メモリ |
| `context` 変数 | `/work/context/` のファイル（`:ro`） |
| `llm_query(prompt)` | **`llm "..."` CLI** |
| `re.findall`/スライス（手動分解） | `grep`/`rg`/`awk`/`sed`/`split`（**LLM 不要の分解**） |
| `Sub_RLM`（depth>1） | **`rlm-sh "q" -c f`**（真の再帰） |
| `FINAL_VAR(var)` | `/work/answer.txt` に書く |

観たい仮説（H1）: 「bash が叩ける + `/work/context/` にファイル + `llm` でサブ呼び出し + `rlm-sh` で再帰」と伝えるだけで、モデルが自発的に「grep/split で分割 → `llm` で MAP → 集約 REDUCE」という RLM 的 MapReduce を組むか。

## アーキテクチャ（既定 v0.4）

- 環境（bash / filesystem / `llm` / 再帰 `rlm-sh`）が核。**ルート脳は差し替え可能**: pure-shell ループ（`llm` 会話を `--cid` 固定）/ Claude Code / Pi CLI。
- サンドボックスは **Docker**（使い捨て）。`llm` は**予算上限付き専用キーを env で持ち、プロバイダへ直結**。
- セキュリティは**比例配分**（ソロ研究プロトタイプ相応）。脅威＝モデル生成 bash の事故（課金爆発・データ破壊）。守りは安く効く2点に集約:
  1. プロバイダ側の**予算ハード上限付き専用キー**
  2. **Docker 使い捨て + 資源制限**（`--rm` / `/work` のみマウント / `--pids-limit` / `--memory` / `--cpus` / `timeout`）＋ `context` の `:ro` マウント
- キー隔離 proxy・egress allowlist 等の多層防御は **productionize / sandbox 共有 / 第三者タスク時の任意オプション**（既定では入れない）。

## 設計書

➡ **[docs/design.md](docs/design.md)** に全設計（アーキテクチャ・RLM 対応表・制御プロトコル・再帰 depth 設計・サンドボックス比較・セキュリティ姿勢・評価計画・実装マイルストーン M0〜M6）。

## 参照プロジェクト

- [`rlm-minimal/`](../rlm-minimal) — RLM の最小実装（REPL + `llm_query`）
- [`rlm/`](../rlm) — フル実装（Docker/E2B/Modal/Daytona の環境抽象）
- [`cheat-at-search-rlm/`](../cheat-at-search-rlm) — RLM ベース検索デモ（harness/validators）
