# rlm-sh

**Bash & Filesystem 上で動く RLM（Recursive Language Models）プロトタイプ**

RLM が *Python REPL + 変数(メモリ) + `llm_query()` 関数* で長コンテキストを再帰処理するのに対し、
rlm-sh は *bash + ファイルシステム(メモリ) + [`llm`](https://github.com/simonw/llm) CLI* で同じ挙動が成立するかを観察する実験台です。

> *LLM agent on REPL w/ variable & `llm_query`* → *LLM agent on bash w/ filesystem & `llm` CLI*

RLM 著者自身が「Python REPL は一つの具体化に過ぎず、本質は **LLM 呼び出しがコード内で行われ出力がメインモデルの文脈に載らない symbolic environment**」と述べている。rlm-sh はその symbolic environment を **bash + filesystem** で具体化したものです。

- ステータス: Draft **v0.4** に基づく M0/M1 実装を開始済み。

## 中核となる対応（RLM → rlm-sh）

| RLM (Python REPL) | rlm-sh (bash + filesystem) |
|---|---|
| REPL 変数（in-memory） | **ファイル**（`/work/` 配下）＝メモリ |
| `context` 変数 | `/context/` のファイル（`:ro`） |
| `llm_query(prompt)` | **`llm "..."` CLI** |
| `re.findall`/スライス（手動分解） | `grep`/`rg`/`awk`/`sed`/`split`（**LLM 不要の分解**） |
| `Sub_RLM`（depth>1） | **`rlm-sh "q" -c f`**（真の再帰） |
| `FINAL_VAR(var)` | `/work/answer.txt` に書く |

観たい仮説（H1）: 「bash が叩ける + `/context/` にファイル + `llm` でサブ呼び出し + `rlm-sh` で再帰」と伝えるだけで、モデルが自発的に「grep/split で分割 → `llm` で MAP → 集約 REDUCE」という RLM 的 MapReduce を組むか。

## アーキテクチャ（既定 v0.4）

- 環境（bash / filesystem / `llm` / 再帰 `rlm-sh`）が核。**ルート脳は差し替え可能**: pure-shell ループ（`llm` 会話を `--cid` 固定）/ Claude Code / Pi CLI。
- サンドボックスは **Docker**（使い捨て）。`llm` は**予算上限付き専用キーを env で持ち、プロバイダへ直結**。
- セキュリティは**比例配分**（ソロ研究プロトタイプ相応）。脅威＝モデル生成 bash の事故（課金爆発・データ破壊）。守りは安く効く2点に集約:
  1. プロバイダ側の**予算ハード上限付き専用キー**
  2. **Docker 使い捨て + 資源制限**（`--rm` / writable `/work` / read-only `/context` / `--pids-limit` / `--memory` / `--cpus` / `timeout`）＋ mount プリフライト
- キー隔離 proxy・egress allowlist 等の多層防御は **productionize / sandbox 共有 / 第三者タスク時の任意オプション**（既定では入れない）。

## 設計書

➡ **[docs/design.md](docs/design.md)** に全設計（アーキテクチャ・RLM 対応表・制御プロトコル・再帰 depth 設計・サンドボックス比較・セキュリティ姿勢・評価計画・実装マイルストーン M0〜M6）。

## M0/M1 Quickstart

```bash
# 1. 予算上限付きの専用キーを使う
export RLMSH_KEY="sk-..."

# 2. sandbox image
python3 rlm-sh/host/sandbox.py build

# 3. NIAH context
python3 rlm-sh/tasks/niah.py --out rlm-sh/.runs/niah-context --lines 10000

# 4. pure-shell root loop
rlm-sh/host/loop_shell.sh \
  --query "Find the magic number hidden in /context/context.txt. Return only the number." \
  --context-dir rlm-sh/.runs/niah-context
```

## 検証したい挙動の叩き方（観察ガイド）

各 run は `rlm-sh/.runs/<run_id>/` に成果物を残します。`<run_id>` は起動ログ `rlm-sh: run_id=...` で確認。

| 見るもの | 場所 / コマンド | 何がわかるか |
|---|---|---|
| **トラジェクトリ** | `rlm-sh/.runs/<id>/transcript.md` | 各ターンの Root 応答 / 実行 bash / sandbox 出力（時系列） |
| **root 会話・トークン・コスト** | `llm logs list -d rlm-sh/.runs/<id>/root.db -n 0 --json` | root LM の会話・model・入出力トークン |
| **モデルが作った中間ファイル** | `rlm-sh/.runs/<id>/work/`（`answer.txt`, `chunks/`, `buffers/`, `notes.md`） | context offloading が起きたか |

### RQ1: 自発的分解（無料の分解 vs `llm` 丸投げ）

```bash
python3 rlm-sh/tasks/metrics.py --run-dir rlm-sh/.runs/<run_id>
```

`free_to_llm_ratio`（grep/rg/awk/sed/split 等の回数 ÷ `llm` サブコール回数）が高いほど「LLM を呼ぶ前に安いシェルで絞った」＝RQ1 が肯定。`rlm_sh_recursions` で再帰回数も確認。

### H1: MapReduce が出るか（grep 一発で解けない課題）

NIAH は `grep` 一発で解けるので **RQ1（無料の分解）** の観察向き。**MapReduce**（split→`llm`→集約）は、全体を読まないと解けない集約タスクで観察する。例: 複数ドキュメントを置いて横断要約・集計させる。

```bash
# /context に複数ファイルを置いて、全体横断の集約を要求する
mkdir -p rlm-sh/.runs/agg-ctx
# （doc_*.md を用意）...
rlm-sh/host/loop_shell.sh \
  --query "Across all files in /context, list every distinct theme and how many documents mention each." \
  --context-dir rlm-sh/.runs/agg-ctx
# 走行後: transcript.md に split→llm→集約 が出るか、metrics.py で llm_subcalls を確認
```

> 専用の集約タスク生成器は M2 で `tasks/` に追加予定。

### 答え合わせ（NIAH の正誤）

```bash
python3 rlm-sh/tasks/niah.py --out rlm-sh/.runs/niah-ctx --answer-out rlm-sh/.runs/niah-ans.txt --lines 200000
rlm-sh/host/loop_shell.sh --query "Find the magic number hidden in /context/context.txt. Return only the number." \
  --context-dir rlm-sh/.runs/niah-ctx --run-id niahrun
diff <(tr -dc 0-9 < rlm-sh/.runs/niahrun/work/answer.txt) <(tr -dc 0-9 < rlm-sh/.runs/niah-ans.txt) \
  && echo MATCH || echo MISMATCH
```

### ablation: 自発性の交絡を切り分ける（設計 §10.4）

同一タスクを 3 段のシステムプロンプトで回し、`metrics.py` で比較する。`--system-prompt` で差し替え:

```bash
for P in min strategy ""; do
  SP=${P:+rlm-sh/conf/system_prompt.$P.md}      # 空 = 既定 conf/system_prompt.md（example 段）
  rlm-sh/host/loop_shell.sh --query "..." --context-dir rlm-sh/.runs/niah-ctx \
    --run-id "ab-${P:-example}" ${SP:+--system-prompt "$SP"}
  python3 rlm-sh/tasks/metrics.py --run-dir "rlm-sh/.runs/ab-${P:-example}"
done
```

- `conf/system_prompt.min.md` … P-min（戦略を一切示さない＝真の自発性）
- `conf/system_prompt.strategy.md` … P-strategy（抽象的指針のみ・具体コマンド例なし）
- `conf/system_prompt.md` … P-example（既定。ツール名・`llm` 使い方まで提示）

P-min で分解が出れば H1 の強い証拠。出なければ「bash 版 RLM はプロンプト依存」という知見。

## 参照プロジェクト

- [`rlm-minimal/`](../rlm-minimal) — RLM の最小実装（REPL + `llm_query`）
- [`rlm/`](../rlm) — フル実装（Docker/E2B/Modal/Daytona の環境抽象）
- [`cheat-at-search-rlm/`](../cheat-at-search-rlm) — RLM ベース検索デモ（harness/validators）
