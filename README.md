# rlm-sh

**Bash & Filesystem 上で動作する RLM (Recursive Language Models) の実験用プロトタイプ**

`rlm-sh` は、LLM による長文コンテキストの再帰処理フレームワーク「RLM」を、**「Bash + ファイルシステム + `llm` CLI」** という開発者にお馴染みの環境で再現し、その挙動を観察するための実験用プロジェクトです。

---

## 💡 概要

### なぜ Bash とファイルシステムなのか？
RLM 論文のオリジナル実装では「Python REPL + メモリ上の変数 + `llm_query()` 関数」が使用されていました。しかし、著者は「Python REPL は具体化の一つに過ぎず、本質は**LLM呼び出しがコード内で行われ、その出力がメインモデルの文脈（コンテキスト窓）を圧迫しないシンボリック環境 (Symbolic Environment)** である」と述べています。

`rlm-sh` は、このシンボリック環境を **Bash とファイルシステム** という最も身近な環境で具体化しました。
これにより、モデルが自発的に「`grep` や `split` でデータを前処理 (無料の分解) し、`llm` を用いて MapReduce 的にデータを集約する」といった RLM 的な最適化行動をとるかを検証します。

---

## 🔄 RLM と rlm-sh の概念対応

RLM (Python REPL) の各要素は、`rlm-sh` では以下のように Bash / ファイルシステムへ写像されます。

| RLM (Python REPL) | rlm-sh (Bash + Filesystem) | 役割・説明 |
| :--- | :--- | :--- |
| **REPL 変数** (In-memory) | **ファイル** (`/work/` 配下) | 中間データや状態を保持する「メモリ」 |
| **`context` 変数** | **ファイル** (`/context/` 配下, 読み取り専用) | 処理対象となる巨大な入力データ |
| **`llm_query(prompt)`** | **`llm "..."` CLI** コマンド | 単発の LLM 問い合わせ |
| **`re.findall` / スライス** | `grep`, `rg`, `awk`, `sed`, `split` 等 | LLM を消費しない「無料の」データ分割・検索 |
| **`Sub_RLM`** (Depth > 1) | **`rlm-sh "q" -c f`** コマンド | 階層的な再帰処理の実行 |
| **`FINAL_VAR(var)`** | `/work/answer.txt` への書き込み | 最終回答の提出 |

---

## 🏗️ アーキテクチャ概要 (v0.4)

* **分離設計**: 「環境（Sandbox）」と「脳（Root Controller）」を明確に分離。ルートモデルの思考ループは pure-shell（`llm` 会話を `--cid` で固定）のほか、Claude Code や Pi CLI などへも差し替え可能です。
* **サンドボックス**: Docker を用いた使い捨て環境。リソース制限（メモリ、CPU、PIDs制限）を施し、入力データを read-only でマウントすることで破壊を防ぎます。
* **現実的なセキュリティ**: 個人研究・ソロ用途のプロトタイプであることを前提とし、複雑な Proxy や Egress 制限は行わず、**「API プロバイダ側での予算上限付き専用キー」＋「Docker 使い捨て」** のシンプルな 2 点に絞って安全性を担保します。

詳細な設計思想や仕様は [docs/design.md](docs/design.md) をご参照ください。

---

## 🚀 クイックスタート (M0-M6)

以下のコマンドは、このリポジトリのルートディレクトリで実行する想定です。

### 1. 専用 API キーの設定
万が一の暴走による課金爆発を防ぐため、プロバイダ側で予算上限を設定した専用の API キーを使用してください。
```bash
export RLMSH_KEY="sk-..."
```

### 2. サンドボックスイメージのビルド
```bash
python3 host/sandbox.py build
```

### 3. テストデータの生成 (NIAH: Needle in a Haystack)
1万行のダミーテキストの中に「魔法の数字」を埋め込んだテストデータを生成します。
```bash
python3 tasks/niah.py --out .runs/niah-context --lines 10000
```

### 4. 実行 (Pure-shell Root Loop)
エージェントを起動し、テキスト内の「魔法の数字」を探させます。
```bash
host/loop_shell.sh \
  --query "Find the magic number hidden in /context/context.txt. Return only the number." \
  --context-dir .runs/niah-context
```

### 5. 再帰呼び出しを有効にした実行 (M3)
`host/loop_shell.sh` は既定で `/work/.spawn/*.json` を監視する host orchestrator を起動します。sandbox 内のモデルが以下のように `rlm-sh` thin client を呼ぶと、host 側が子 sandbox を起動し、結果を親プロセスの stdout に返します。
```bash
rlm-sh "Summarize this chunk" --context chunks/c01.txt
```

主な制御オプション:
```bash
host/loop_shell.sh \
  --query "..." \
  --context-dir .runs/ctx \
  --max-depth 2 \
  --max-spawns 32 \
  --child-timeout 900
```

安全契約:
- `--context` は親 `/work` からの相対パスのみ受け付けます。
- `..` や絶対パスは host orchestrator が拒否します。
- 子 context は共有マウントではなく snapshot copy として作成され、hash が記録されます。

---

## ✅ 動作検証手順

検証は、API 課金を伴わない smoke test と、実 LLM を呼ぶ live test に分けて実行してください。`--live-llm` を付けない `m0-check` は `llm --version` までの確認で、モデル API は呼びません。

### 1. 静的検証
```bash
python3 -m py_compile \
  host/sandbox.py host/loop_utils.py host/validators.py host/orchestrator.py \
  host/adapters.py host/backends.py tasks/niah.py tasks/metrics.py \
  tasks/mapreduce.py bin/rlm-sh

bash -n host/loop_shell.sh bin/submit
git diff --check
```

### 2. Backend readiness
```bash
python3 host/backends.py list
python3 host/backends.py check --backend docker
python3 host/backends.py check --backend docker-sandboxes
python3 host/backends.py check --backend local-unsafe
```

`docker-sandboxes` で `Server Version: Unavailable` が出る場合は、`sbx login` と Docker Sandboxes daemon の状態を確認してください。

### 3. Sandbox image / template build
```bash
# Docker backend
python3 host/sandbox.py build --backend docker --image rlm-sh-sandbox:dev

# Docker Sandboxes backend: Docker image を sbx template store に load する
python3 host/sandbox.py build --backend docker-sandboxes --image rlm-sh-sandbox:dev
```

### 4. M0 smoke: sandbox wiring
```bash
export RLMSH_KEY=fake-key

python3 host/sandbox.py m0-check \
  --backend docker \
  --run-dir .runs/verify-m0-docker \
  --run-id verify-m0-docker \
  --api-key-env RLMSH_KEY

python3 host/sandbox.py m0-check \
  --backend docker-sandboxes \
  --run-dir .runs/verify-m0-sbx \
  --run-id verify-m0-sbx \
  --api-key-env RLMSH_KEY
```

期待値は `llm, version 0.31` が表示されることです。実 API 疎通まで確認する場合だけ、予算上限付きキーを設定して `--live-llm` を付けます。

```bash
export RLMSH_KEY="sk-..."
python3 host/sandbox.py m0-check --backend docker --live-llm --api-key-env RLMSH_KEY
```

### 5. Backend exec smoke
```bash
mkdir -p .runs/verify-local/context .runs/verify-local/work
printf 'local context ok\n' > .runs/verify-local/context/context.txt
export RLMSH_KEY=fake-key

HANDLE="$(
  python3 host/sandbox.py start \
    --backend local-unsafe \
    --work-dir .runs/verify-local/work \
    --context-dir .runs/verify-local/context \
    --run-id verify-local \
    --api-key-env RLMSH_KEY
)"

python3 host/sandbox.py exec \
  --container "$HANDLE" \
  --timeout 5 \
  -- 'cat /context/context.txt > /work/answer.txt; cat /work/answer.txt'
```

`local-unsafe` は host 上で実行するデバッグ backend です。隔離検証には Docker または Docker Sandboxes を使ってください。

### 6. M1 live: root loop
```bash
export RLMSH_KEY="sk-..."
# host 側 llm CLI も root model を呼べるように設定しておく
llm logs status

python3 tasks/niah.py --out .runs/niah-context --answer-out .runs/niah-answer.txt --lines 10000

host/loop_shell.sh \
  --build \
  --backend docker \
  --query "Find the magic number hidden in /context/context.txt. Return only the number." \
  --context-dir .runs/niah-context
```

成功時は標準出力に数字だけが出ます。生成時の正解と照合します。

```bash
cat .runs/niah-answer.txt
python3 tasks/metrics.py --run-dir .runs/<run_id>
```

Docker Sandboxes で同じ確認をする場合は `--backend docker-sandboxes` を指定します。初回は template load のため `--build` を付けてください。

### 7. M2-M4 observation
```bash
python3 tasks/mapreduce.py generate --out .runs/mr-context --docs 48

host/loop_shell.sh \
  --backend docker \
  --query "$(cat .runs/mr-context/query.txt)" \
  --context-dir .runs/mr-context

python3 tasks/mapreduce.py observe --run-dir .runs/<run_id>
python3 tasks/mapreduce.py score \
  --run-dir .runs/<run_id> \
  --expected .runs/mr-context/expected.json
python3 tasks/metrics.py --run-dir .runs/<run_id>
```

確認対象は `transcript.md`、`orchestrator_events.jsonl`、`context_hash.*.json`、`spawns/*/manifest.json`、`children/*/` です。`rlm-sh "..." --context ...` が使われた場合は、子 run と `parent_call_id` correlation も metrics に出ます。

---

## 🔍 実験・観察ガイド

実行結果は `.runs/<run_id>/` に保存されます（`<run_id>` は実行ログ `rlm-sh: run_id=...` で確認できます）。

| 観察対象 | 確認方法 / ファイル | わかること |
| :--- | :--- | :--- |
| **実行の軌跡** | `.runs/<id>/transcript.md` | ターンごとの Root 応答、実行された Bash、Sandbox の出力履歴 |
| **トークン・コスト** | `llm logs list -d .runs/<id>/root.db -n 0 --json` | Root モデルとの対話履歴、使用モデル、消費トークン数 |
| **中間ファイル** | `.runs/<id>/work/` 配下 | `answer.txt` や、モデルが自発的に作成した chunk やバッファ |

### 📊 自発的分解メトリクスの集計
モデルがどの程度「安価な Bash コマンド」を活用し、「高価な LLM」を節約できたかを測定します。
```bash
python3 tasks/metrics.py --run-dir .runs/<run_id>
```
* **`free_to_llm_ratio`**: `grep` 等の非 LLM コマンド回数 ÷ `llm` サブコール回数。この比率が高いほど、LLM に丸投げせず、軽量なコマンドで賢くデータを絞り込んだ（自発的分解が行われた）ことを示します。
* M3/M4 以降は `children/` 配下の子 run、`orchestrator_events.jsonl`、context hash 検証結果、`parent_call_id` correlation もまとめて集計されます。

---

## 🧪 応用的な実験シナリオ

### 1. MapReduce の観察（集約タスク）
単一の `grep` では解決できない、全体を読み解く必要があるタスク（複数ファイルの横断集計など）を与え、エージェントが「ファイルを分割 ➡️ 並列で LLM 処理 (Map) ➡️ 結果をマージして集計 (Reduce)」という一連のパイプラインを組み立てるかを観察します。
```bash
python3 tasks/mapreduce.py generate --out .runs/mr-context --docs 48

host/loop_shell.sh \
  --query "$(cat .runs/mr-context/query.txt)" \
  --context-dir .runs/mr-context

python3 tasks/mapreduce.py observe --run-dir .runs/<run_id>
python3 tasks/mapreduce.py score \
  --run-dir .runs/<run_id> \
  --expected .runs/mr-context/expected.json
```

### 2. Ablation Study: プロンプト誘導の排除
システムプロンプトの具体度を 3 段階に変化させ、自発的に分解が発生しているかを検証します。
```bash
for P in min strategy ""; do
  SP=${P:+conf/system_prompt.$P.md}      # 空 = 既定の system_prompt.md (具体例あり)
  host/loop_shell.sh --query "..." --context-dir .runs/niah-ctx \
    --run-id "ab-${P:-example}" ${SP:+--system-prompt "$SP"}
  python3 tasks/metrics.py --run-dir ".runs/ab-${P:-example}"
done
```
* **P-min** (`system_prompt.min.md`): 戦略を一切示さない（真の自発性を測定）。
* **P-strategy** (`system_prompt.strategy.md`): 抽象的な指針のみを提示。
* **P-example** (`system_prompt.md`): ツール名や `llm` の使用例まで提示する既定のプロンプト。

### 3. 外部脳 adapter (M5)
`pure-shell` 以外の Claude Code / Codex / Pi などは、adapter wrapper から command template として接続できます。template は sandbox 内で実行され、`{prompt}` には `/work/adapter_prompt.txt` が入ります。
```bash
python3 host/adapters.py list

python3 host/adapters.py run \
  --adapter codex \
  --context-dir .runs/mr-context \
  --query "$(cat .runs/mr-context/query.txt)" \
  --command-template 'codex exec --full-auto -- "$(cat {prompt})"'
```

### 4. Backend 差し替え (M6)
既定は Docker です。`docker-sandboxes` は `sbx create/exec/rm` を使い、Docker Sandboxes の lifecycle と workspace governance に乗せて同じ `/work` / `/context` 契約を提供します。`local-unsafe` は Docker が使えない時のデバッグ用で、host 上でコマンドを実行し `/work` と `/context` を実パスに置換します。
```bash
python3 host/backends.py list
python3 host/backends.py check --backend docker
python3 host/backends.py check --backend docker-sandboxes

# Docker Sandboxes backend は build 時に Docker image を sbx template store へ load する
python3 host/sandbox.py build --backend docker-sandboxes --image rlm-sh-sandbox:dev

host/loop_shell.sh \
  --backend docker-sandboxes \
  --query "..." \
  --context-dir .runs/niah-context

host/loop_shell.sh \
  --backend local-unsafe \
  --query "..." \
  --context-dir .runs/niah-context
```

---

## 🔗 参照プロジェクト

* [rlm-minimal/](../rlm-minimal) — Python REPL を用いた RLM の最小実装
* [rlm/](../rlm) — 複数環境 (Docker/Modal/Daytona) に対応したフル機能の RLM 実装
* [cheat-at-search-rlm/](../cheat-at-search-rlm) — 特許検索を題材とした RLM 実装例
