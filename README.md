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

## 🚀 クイックスタート (M0/M1)

### 1. 専用 API キーの設定
万が一の暴走による課金爆発を防ぐため、プロバイダ側で予算上限を設定した専用の API キーを使用してください。
```bash
export RLMSH_KEY="sk-..."
```

### 2. サンドボックスイメージのビルド
```bash
python3 rlm-sh/host/sandbox.py build
```

### 3. テストデータの生成 (NIAH: Needle in a Haystack)
1万行のダミーテキストの中に「魔法の数字」を埋め込んだテストデータを生成します。
```bash
python3 rlm-sh/tasks/niah.py --out rlm-sh/.runs/niah-context --lines 10000
```

### 4. 実行 (Pure-shell Root Loop)
エージェントを起動し、テキスト内の「魔法の数字」を探させます。
```bash
rlm-sh/host/loop_shell.sh \
  --query "Find the magic number hidden in /context/context.txt. Return only the number." \
  --context-dir rlm-sh/.runs/niah-context
```

---

## 🔍 実験・観察ガイド

実行結果は `rlm-sh/.runs/<run_id>/` に保存されます（`<run_id>` は実行ログ `rlm-sh: run_id=...` で確認できます）。

| 観察対象 | 確認方法 / ファイル | わかること |
| :--- | :--- | :--- |
| **実行の軌跡** | `rlm-sh/.runs/<id>/transcript.md` | ターンごとの Root 応答、実行された Bash、Sandbox の出力履歴 |
| **トークン・コスト** | `llm logs list -d rlm-sh/.runs/<id>/root.db -n 0 --json` | Root モデルとの対話履歴、使用モデル、消費トークン数 |
| **中間ファイル** | `rlm-sh/.runs/<id>/work/` 配下 | `answer.txt` や、モデルが自発的に作成した chunk やバッファ |

### 📊 自発的分解メトリクスの集計
モデルがどの程度「安価な Bash コマンド」を活用し、「高価な LLM」を節約できたかを測定します。
```bash
python3 rlm-sh/tasks/metrics.py --run-dir rlm-sh/.runs/<run_id>
```
* **`free_to_llm_ratio`**: `grep` 等の非 LLM コマンド回数 ÷ `llm` サブコール回数。この比率が高いほど、LLM に丸投げせず、軽量なコマンドで賢くデータを絞り込んだ（自発的分解が行われた）ことを示します。

---

## 🧪 応用的な実験シナリオ

### 1. MapReduce の観察（集約タスク）
単一の `grep` では解決できない、全体を読み解く必要があるタスク（複数ファイルの横断要約など）を与え、エージェントが「ファイルを分割 ➡️ 並列で LLM 処理 (Map) ➡️ 結果をマージして集計 (Reduce)」という一連のパイプラインを組み立てるかを観察します。
```bash
# /context に複数ファイルを置いて、全体横断の集約を要求する
mkdir -p rlm-sh/.runs/agg-ctx
# (ここに対象となる doc_*.md を用意)
rlm-sh/host/loop_shell.sh \
  --query "Across all files in /context, list every distinct theme and how many documents mention each." \
  --context-dir rlm-sh/.runs/agg-ctx
```

### 2. Ablation Study: プロンプト誘導の排除
システムプロンプトの具体度を 3 段階に変化させ、自発的に分解が発生しているかを検証します。
```bash
for P in min strategy ""; do
  SP=${P:+rlm-sh/conf/system_prompt.$P.md}      # 空 = 既定の system_prompt.md (具体例あり)
  rlm-sh/host/loop_shell.sh --query "..." --context-dir rlm-sh/.runs/niah-ctx \
    --run-id "ab-${P:-example}" ${SP:+--system-prompt "$SP"}
  python3 rlm-sh/tasks/metrics.py --run-dir "rlm-sh/.runs/ab-${P:-example}"
done
```
* **P-min** (`system_prompt.min.md`): 戦略を一切示さない（真の自発性を測定）。
* **P-strategy** (`system_prompt.strategy.md`): 抽象的な指針のみを提示。
* **P-example** (`system_prompt.md`): ツール名や `llm` の使用例まで提示する既定のプロンプト。

---

## 🔗 参照プロジェクト

* [rlm-minimal/](../rlm-minimal) — Python REPL を用いた RLM の最小実装
* [rlm/](../rlm) — 複数環境 (Docker/Modal/Daytona) に対応したフル機能の RLM 実装
* [cheat-at-search-rlm/](../cheat-at-search-rlm) — 特許検索を題材とした RLM 実装例
