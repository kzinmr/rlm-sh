# rlm-sh 設計書 — Bash & Filesystem 上で動く RLM プロトタイプ

> **一行で:** RLM が「Python REPL + 変数(メモリ) + `llm_query()` 関数」で長コンテキストを再帰的に処理するのに対し、`rlm-sh` は **「bash + ファイルシステム(メモリ) + `llm` CLI」** で同じ挙動を実現できるか、を観察するための実験用プロトタイプ。
>
> *LLM agent on REPL w/ variable & `llm_query`*  →  *LLM agent on bash w/ filesystem & `llm` CLI*

- ステータス: Draft v0.4（設計フェーズ・レビュー反映済み）
- **v0.4 決定（セキュリティ姿勢の見直し）**: 本プロジェクトは**ソロ・自分のマシンで回す研究プロトタイプ**であり、`llm` 自体が個人用途の単一ユーザ CLI（平文キー保存・自前の隔離なし）。よって**多層防御は過剰**と判断し、既定を **「実キーを env で sandbox に直接渡す + プロバイダ側の予算ハード上限 + Docker 使い捨て」** に簡素化する（§4.4, §5.3, §9）。**自作 proxy / キー隔離 / egress allowlist は「productionize・sandbox 共有・マルチテナント時の任意オプション」に降格**（§5.4）。これにより v0.2 の P0（`--network none` 矛盾）は前提ごと解消（§8, §9）。
- ~~v0.3 決定（proxy 第一実装）~~ → v0.4 で**任意オプションに降格**。
- 対象読者: 本プロジェクトの実装者・実験者
- 参照実装: [`rlm-minimal/`](../../rlm-minimal)、[`rlm/`](../../rlm)、[`cheat-at-search-rlm/`](../../cheat-at-search-rlm)
- 一次資料: RLM 論文 [arXiv:2512.24601](https://arxiv.org/abs/2512.24601)、[RLM blog](https://alexzhang13.github.io/blog/2025/rlm/)、[simonw/llm](https://github.com/simonw/llm)

### 検証済み前提（`llm` 0.31, 実機確認）

`llm` CLI を環境にインストール・キー設定し、以下を確認済み（本設計が依存する仕様）:

- フラグ実在: `-c/--continue`、`--cid/--conversation TEXT`、`-f/--fragment`、`--schema`、`-d/--database FILE`、`-n/--no-log`、`--log`。
- `llm logs -n 1 --json` の各レコードは次を含む: `id`（呼び出し単位 ID）、`conversation_id`、`model`/`resolved_model`、`input_tokens`/`output_tokens`/`token_details`、`duration_ms`、`datetime_utc`、`prompt`/`response`、`schema_json` 等。
- ログ DB 既定パス: `~/Library/Application Support/io.datasette.llm/logs.db`（`llm logs path` で確認）。`-d` で DB を分離可能。
- `extra-openai-models.yaml` は `api_base` / `api_key_name` 対応。
- ⇒ 「`llm` CLI 仕様が不明」という懸念は取り下げ。**`--cid` があるので root 会話は明示固定する**（§5.5, §6.1）。

### v0.2 で反映したレビュー（要約）

| # | 重大度 | 指摘 | 反映先 |
|---|---|---|---|
| 1 | P0 | `--network none` と Host Gateway 通信は両立しない | §5.1, §8, §9, §11.2(M0) |
| 2 | P0 | `llm -c` は root/subcall が混線 → `--cid` 固定 + DB 分離が必須 | §5.3, §5.5-A, §6.1 |
| 3 | P1 | `/spawn` の `context_path` の path containment / snapshot 契約が未設計 | §7 |
| 4 | P1 | ガードレールが M4 では遅い → 最低限を M1 へ前倒し | §9, §11.2 |
| 5 | P1 | root controller の実行場所が曖昧 → MVP を一つに固定 | §5.5-A, §6.1 |
| 6 | P2 | H1/RQ1 の「自発性」がプロンプト誘導で交絡 → ablation 追加 | §10.1, §10.4 |
| 7 | P2 | 観測の相関 ID 設計不足 → 共通相関 ID を導入 | §5.4, §10.2 |

---

## 目次

1. [背景と目的](#1-背景と目的)
2. [中核となる仮説とリサーチクエスチョン](#2-中核となる仮説とリサーチクエスチョン)
3. [RLM 概念の対応表（REPL → bash）](#3-rlm-概念の対応表repl--bash)
4. [アーキテクチャ全体像](#4-アーキテクチャ全体像)
5. [コンポーネント詳細](#5-コンポーネント詳細)
6. [制御プロトコル（ルートループ）](#6-制御プロトコルルートループ)
7. [再帰（depth）設計](#7-再帰depth設計)
8. [サンドボックス選定](#8-サンドボックス選定)
9. [セキュリティ・state integrity・ガードレール](#9-セキュリティstate-integrityガードレール)
10. [観察したい挙動と評価](#10-観察したい挙動と評価)
11. [リポジトリ構成と実装計画](#11-リポジトリ構成と実装計画)
12. [リスクと未解決問題](#12-リスクと未解決問題)
13. [付録: 例セッション](#13-付録-例セッション)

---

## 1. 背景と目的

### 1.1 RLM とは（要約）

Recursive Language Models（Zhang, Kraska, Khattab, MIT CSAIL）は、`llm.completion(prompt)` を `rlm.completion(prompt)` に置き換える推論パラダイム。核心は次の構成にある（[wiki: rlm-recursive-language-models](https://arxiv.org/abs/2512.24601)）:

1. **コンテキストを環境変数としてオフロード** — 巨大な入力を REPL 内の `context` 変数として保持し、ルート LM は全文を一度に見ない。
2. **プログラム的分解（CodeAct）** — LM が Python コードを書いて `context` を覗き・スライスし・変換する。
3. **再帰的サブコール** — `llm_query(prompt)` で短いプロンプトに対するサブ LM 呼び出しを REPL 内から発行する。
4. **最終合成** — ルート LM が結果を集約し答えを出す。

著者自身の重要な明確化（2026-05 / wiki より）:

> RLM の新規性は個々の部品ではなく **「構成（composition）と十分性（sufficiency）」** にある。Python REPL は *一つの具体化* に過ぎず、本質ではない。本質は **「LLM 呼び出しがコード内で行われ、その出力がメインモデルのコンテキストに載らない symbolic environment」** である。

この「REPL は本質ではない」という著者の立場が、本プロジェクトの出発点である。

### 1.2 本プロジェクトの狙い

**Python REPL を別の symbolic environment＝bash + filesystem に置き換えても、RLM 的挙動（コンテキストのオフロード・プログラム的分解・再帰的サブコール）は成立するか** を観察する。

- 実行環境: Python REPL → **bash シェル**
- メモリ（変数）: in-memory な REPL `locals` → **ファイルシステム上のファイル**
- サブ LM 呼び出し: `llm_query()` 関数 → **`llm` CLI（simonw/llm）コマンド**
- 再帰: `Sub_RLM` / `rlm_query` → **`rlm-sh` 自身を bash から再帰呼び出し**

これは「コーディングエージェントは RLM か？」という wiki 上の議論（Dynamic Workflows ≈ scaffold-level RLM、"Claude Code can function as an RLM"）を、**最小構成で再現・観察する実験台**でもある。bash + filesystem + `llm` こそ、コーディングエージェントが実際に使っている環境そのものだからだ。

### 1.3 非目標（Non-goals）

- 本番運用・高スループット・SLA。あくまで**挙動観察用プロトタイプ**。
- RLM-native な学習（post-training）。スキャフォールドレベルの観察に留める。
- ベンチマーク SOTA の更新。評価は「挙動が出るか」「どこで壊れるか」の定性観察が主。

---

## 2. 中核となる仮説とリサーチクエスチョン

### 2.1 仮説

> **H1（成立仮説）**: ルート LM に「bash が叩ける + `context/` 以下にファイルがある + `llm` でサブ呼び出しできる + `rlm-sh` で再帰できる」と伝えるだけで、モデルは自発的に「ファイルを grep/split で分割 → 各チャンクを `llm` に投げ → 結果ファイルを集約」という RLM 的 MapReduce を組み立てる。

### 2.2 bash 版ならではの観察ポイント

| 観点 | REPL 版 | bash 版で何が変わるか |
|---|---|---|
| **無料の分解手段** | `re.findall`, スライス | `grep`/`rg`/`awk`/`sed`/`split`/`jq`/`head`。**LLM を呼ばずに巨大コンテキストを検索・分割できる**。ripgrep なら 1GB も一瞬。RLM の「manual decomposition」が桁違いに強力になるはず。 |
| **メモリの永続性** | in-memory `locals`、dill でシリアライズが必要 | ファイルは**そのまま永続・grep 可能・人間が覗ける**。「context offloading to file」がデフォルト挙動になる（RLM 論文で coding agent を底上げした手法そのもの）。 |
| **観測性** | 自前のコスト/ログ実装が必要 | `llm logs`（SQLite）に全呼び出しが自動記録。コスト・トークン・会話が無料で観測可能。 |
| **合成性** | 関数合成 | `\|`（パイプ）で `cat huge.txt \| llm -s "summarize"` のようにストリーム合成。 |
| **再帰** | `Sub_RLM` クラス置換 | `rlm-sh "subquery" --context f.txt` を bash から呼ぶだけ。**真の再帰が自然に書ける**。 |

### 2.3 観察したい失敗モード（RQ）

- **RQ1 — 自発的分解**: モデルは grep/split で「無料の」分解を使うか、それとも何でも `llm` に丸投げして浪費するか？
- **RQ2 — クオート地獄**: 特殊文字を含むプロンプトを `llm "..."` に渡すときのシェルのクオート/エスケープ崩れ（REPL の Python 文字列には無い固有の失敗）。ヒアドキュメントや「プロンプトを一旦ファイルに書いて `llm < file`」で緩和されるか？
- **RQ3 — state integrity**: モデルは自分のスキャフォールド（`answer.txt`、`context/`）を `rm`/上書きで壊すか？（RLM の "agents can mutate/delete REPL variables" 問題の bash 版）
- **RQ4 — コスト爆発**: `&` での並列 `llm` 呼び出しが fork bomb 化しないか。
- **RQ5 — 切り詰めとコンテキスト腐敗**: bash の stdout をルート LM に返す際の truncation 戦略は適切か。
- **RQ6 — 深さスケーリング**: depth>1（`rlm-sh` 再帰）で OOLONG-Pairs 的な情報密度の高いタスクが改善するか（RLM v3 の主要知見の再現）。
- **RQ7 — ルート脳の違い**: 同じ環境を **pure-shell ループ（`--cid` 固定）** で回す場合と **Claude Code / Pi CLI** をルートにする場合で、RLM 的挙動の出方はどう違うか。

---

## 3. RLM 概念の対応表（REPL → bash）

参照: [`rlm-minimal/rlm/repl.py`](../../rlm-minimal/rlm/repl.py)、[`rlm/rlm/environments/docker_repl.py`](../../rlm/rlm/environments/docker_repl.py)

| RLM（Python REPL） | rlm-sh（bash + filesystem） | 備考 |
|---|---|---|
| ルート LM が ` ```repl ` ブロックを出力 | ルート LM が **bash コマンド**を発行 | 伝達方式は §6（agent-agnostic） |
| `exec()` による REPL | **bash シェル（コンテナ内）** | Python は「bash 内の一ツール」に格下げ |
| `context` 変数（in-memory） | **`/context/`** 配下のファイル群（例: `/context/context.txt`） | 巨大入力は read-only ファイルとして配置 |
| REPL `locals`（作業変数） | **`/work/` 配下の作業ファイル**（`chunks/`, `buffers/`, `notes.md`…） | これが「メモリ」 |
| `llm_query(prompt)` | **`llm "prompt"`** / `cat f \| llm -s "..."` | simonw/llm。詳細 §5.3 |
| `llm_query_batched(prompts)` | `ls chunks/* \| xargs -P4 -I{} llm ... ` / `&` 並列 | バッチ＝シェル並列 |
| サブ LM（500K char 窓） | `llm -m <large-context-model> -f <file>` | `-f`/stdin で大入力 |
| `Sub_RLM` / `rlm_query`（depth>1） | **`rlm-sh "q" --context f`**（再帰呼び出し） | §7 |
| `FINAL(answer)` / `FINAL_VAR(var)` | **`/work/answer.txt` に書く** or `submit <<<"..."` | §6.4 |
| 出力 8192 char 切り詰め | bash stdout の **head/wc 切り詰め**（ファイルは無制限） | §6.3 |
| 反復ループ（max_iterations） | 同（ルートループ） | §6.1 |
| `historical_results`（可変メモリ） | `/work/history.md`（追記） | cheat-at-search 由来 |
| `patent_search` 等の immutable tool | コンテナ内 CLI / `/work/bin/` のスクリプト | §5.2 |
| RESERVED_TOOL_NAMES（上書き禁止） | **予約ファイル/パス**（`/context/` は `:ro`、`answer.txt` は上書き可） | §9 |
| Modal/Daytona/E2B/Docker env | **Docker（第一候補）** / E2B / local | §8 |

**設計上の含意:** RLM では「変数 ＝ メモリ」「関数 ＝ ツール」だった。bash 版では **「ファイル ＝ メモリ」「コマンド ＝ ツール」** になる。`llm` はコマンド、`rlm-sh` はコマンド、`context` はファイル。この素直な写像が成立するかが H1。

---

## 4. アーキテクチャ全体像

### 4.1 設計原則: 「環境」と「ルート脳」の分離

ユーザ要件（「Python REPL を必須と仮定しない」「Pi cli や claude code cli から呼び出す形にも対応」）と、RLM 著者の「REPL は本質ではない」という立場から、**rlm-sh の核は環境であり、ルートコントローラは差し替え可能なアダプタ**とする。

既定（v0.4・プロトタイプ相応のシンプル構成）:

```
   ┌──────────────┐          ┌─────────────────────────────────────────┐
   │ Root Controller│◄────────►│  HOST                                   │
   │ (差し替え可能) │  docker  │   - Sandbox Orchestrator                │
   │ A) pure-shell  │   exec   │       子sandbox spawn / depth メータ    │
   │   (--cid 固定) │          │   - run 起動 / answer 検出 / ログ収集   │
   │ B) Claude Code │          └─────────────────────────────────────────┘
   │ C) Pi / codex  │             │ docker run（使い捨て・資源制限）
   │ D) Python harness│           ▼
   └──────────────┘   ┌──────────────────────────────────────────────┐
                      │  SANDBOX（Docker, 使い捨て）                   │
                      │   bash + coreutils + rg/jq/awk/sed             │
                      │                                                │
                      │   $ llm -m gpt-5-mini "..."  ──HTTP──►  プロバイダ
                      │       （予算上限付き専用キーを env で保持）     │ (api.openai.com 等)
                      │   $ rlm-sh "q" -c f  → Host に子sandbox 依頼    │
                      │                                                │
                      │   /context/(ro)  (= 入力 / immutable context)   │
                      │   /work/  (= メモリ / filesystem)              │
                      │     answer.txt  history.md                     │
                      │     chunks/  buffers/  notes.md                │
                      └──────────────────────────────────────────────┘
   守り = ①プロバイダ側 予算ハード上限 ②Docker 使い捨て+資源制限（§9）
```

任意の強化（productionize / sandbox 共有 / マルチテナント時のみ・§5.4）: sandbox から実キーを抜き、`llm` の `api_base` を **Host の自作 proxy** に向け、internal network で egress を絞る。研究目的には不要なので既定では使わない。

### 4.2 層構成（既定）

1. **Sandbox 層（Docker・使い捨て）**: bash + filesystem + Unix ツール + `llm`（**予算上限付き専用キーを env で保持**）+ `rlm-sh`。→ §5.1, §5.3
2. **Host**: ①run の起動・`answer.txt` 検出・ログ収集（root ループはここで動く・§5.5-A）②Sandbox Orchestrator（`rlm-sh` 再帰の子 sandbox 生成・depth 制御・§7）。**実キーを中継する proxy は既定では置かない**（任意オプション・§5.4）。
3. **Root Controller（差し替え可能）**: サンドボックス内 bash を駆動する「脳」。デフォルトは pure-shell ループ（`--cid` 固定）。外部エージェント CLI（Claude Code / Pi / codex）や薄い Python ハーネスも差せる。→ §5.5

### 4.3 「環境は同一、脳だけ差し替え」がもたらす実験的価値

同一の sandbox（bash/filesystem/`llm`/`rlm-sh`）を固定し、ルート脳だけ差し替えられるので、**「Claude Code は本当に RLM 的に振る舞うか？」を環境を統制したまま観察**できる（RQ7）。pure-shell ループは RLM 論文への最忠実な再現、Claude Code/Pi は「現実のコーディングエージェントが RLM 化するか」の観察。

### 4.4 プロトタイプのセキュリティ姿勢（proportionate / v0.4）

過剰防御を避けるため、**脅威モデルを明示**して守りを比例配分する。

- **本プロジェクトの前提**: ソロ・自分の信頼できるマシン・研究観察目的。`llm` は個人用途の単一ユーザ CLI（平文キー、隔離なし）。
- **実在する脅威 = モデル生成 bash の“事故”**（悪意ある外部攻撃者ではない）:
  - 暴走ループによる**課金爆発**（RQ4）／無限再帰
  - 誤った `rm -rf` 等での**自分のデータ破壊**
- **既定の守り（安く効く2点・M1 必須）**:
  1. **プロバイダ側の予算ハード上限**を張った**専用・低価値キー**を使う（漏れ/暴走の被害＝その予算枠に限定）。
  2. **Docker 使い捨て + 資源制限**（`--rm` + writable `/work` と read-only `/context` だけマウント、`--pids-limit`/`--memory`/`--cpus`、`docker exec` に `timeout`）。
- **既定で“やらない”こと（過剰なので productionize 時に回す・§5.4）**: 実キーのサンドボックス排除、自作 proxy 経由、egress allowlist、per-run 仮想キー、ログのキースクラブ。これらは「キーを攻撃者に流出させない／マルチテナントで隔離する」ための防御で、ソロ研究では実害が小さい。
- **線引き（いつ強化するか）**: ① sandbox を他人に開放する ② 共有/CI で回す ③ 広域キーしか使えない ④ 信頼できない第三者のタスクを流す — のいずれかになったら §5.4 の proxy + キー隔離を有効化する。

---

## 5. コンポーネント詳細

### 5.1 Sandbox 層（Docker, 第一候補）

`rlm/rlm/environments/docker_repl.py` を踏襲しつつ、**Python の `exec` ではなく bash を実行単位**にする。

- ベースイメージ: `python:3.11-slim` ではなく、**bash 中心のツール充実イメージ**を自前ビルド（`Dockerfile.sandbox`）。
- 同梱ツール: `bash`, coreutils, `ripgrep`, `jq`, `gawk`, `sed`, `grep`, `curl`, `python3`(任意のツールとして), そして **`llm`（simonw/llm）** と **`rlm-sh`(thin client)**。
- ワークスペース: `/work` をホストの run ディレクトリに writable mount（`-v run/work:/work`）。これが「メモリ」。入力は sibling mount の `/context:ro` に置く。
- リソース制限: `--memory`, `--cpus`, `--pids-limit`, `--read-only`（`/work` 以外）。
- ネットワーク（**v0.4 簡素化**）: 既定は**通常の bridge ネットワーク**。`llm` がコンテナ内キーで**プロバイダ API（`api.openai.com` 等）に直接**到達する。`--network none` にすると `llm` が API に出られないので使わない。これで v0.2 の P0（`--network none` vs Gateway 矛盾）は前提ごと消える。
  - 任意の強化（§5.4 の proxy を使う場合のみ）: 専用 `--internal` network + Gateway sidecar、または egress allowlist で「Gateway/プロバイダだけ到達可」に絞る。研究プロトタイプでは不要。
- 使い捨て: `--rm`。状態はマウントした `/work` にのみ残る。
- 起動プリフライト: コンテナ起動直後に `/work` への probe 書き込みと `/context` の read-only 確認を行い、Docker Desktop/rootless の bind mount 問題を `submit` 実行前に検出する。

```dockerfile
# Dockerfile.sandbox （スケッチ）
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash coreutils grep sed gawk ripgrep jq curl ca-certificates python3 python3-pip pipx \
 && rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.local/bin:${PATH}"
ENV LLM_USER_PATH="/work/.llm"
RUN pipx install 'llm==0.31'
COPY bin/rlm-sh /usr/local/bin/rlm-sh
COPY bin/submit /usr/local/bin/submit
RUN chmod +x /usr/local/bin/rlm-sh /usr/local/bin/submit
WORKDIR /work
CMD ["bash"]
```

- **キーの渡し方（既定）**: 実キーはイメージに焼かず、**起動時に env で注入**（`docker run -e OPENAI_API_KEY=$RLMSH_KEY ...`）。`$RLMSH_KEY` は**予算ハード上限を張った専用キー**（§9）。イメージ layer に残らず、`--rm` で破棄される。`llm` は env から直接読む（§検証済み前提）。

### 5.2 Filesystem = メモリ

`/context` と `/work` のレイアウト規約（モデルにシステムプロンプトで提示）:

```
/context/          # 入力（巨大コンテキスト）。read-only mount
  context.txt        or  doc_0001.md, doc_0002.md, ...

/work/
  answer.txt       # ★ 最終回答をここに書くと終了（FINAL の bash 版）
  history.md       # 追記式の作業ログ（historical_results 相当・可変メモリ）
  chunks/          # モデルが split/grep で作る中間チャンク
  buffers/         # サブ呼び出し結果のバッファ（MapReduce の中間生成物）
  notes.md         # スクラッチ
  bin/             # （任意）ドメイン固有ツールのスクリプト
```

- **context は immutable**: 既定は nested mount を避けた **`-v ...:/context:ro`（read-only sibling mount）** で破壊を構造的に防ぐ（§9.2）。毎ステップのハッシュ照合・復元は M4 の任意強化。
- ファイルは **無制限サイズ**で保持。ルート LM に返すのは「ファイル一覧」「`wc -l`」「`head`」など要約のみ（§6.3）。

### 5.3 `llm` CLI = `llm_query`（simonw/llm の活用）

`llm` は本プロジェクトの心臓。`llm_query()` を CLI コマンドへ写像する。

| RLM の使い方 | rlm-sh での書き方 | 活用する `llm` 機能 |
|---|---|---|
| `llm_query(prompt)` | `llm "$prompt"` | one-shot prompt |
| `llm_query(big_chunk)` | `cat chunks/c01 \| llm -s "この章を要約"` | stdin をプロンプト入力に |
| system 付き | `llm -s "あなたは抽出器" "..."` | `-s/--system` |
| モデル指定（安いサブモデル） | `llm -m gpt-5-mini "..."` | `-m`（プロバイダのモデル名を直接指定） |
| 大ファイル添付 | `llm -f /context/doc_0007.md "要点は?"` | fragments/attachments |
| バッチ並列 | `ls chunks/* \| xargs -P4 -I{} sh -c 'llm -s "..." < {} > buffers/$(basename {}).out'` | シェル並列 = `llm_query_batched` |
| 構造化出力 | `llm --schema 'name,score int' "..."` | schemas（JSON 強制） |
| 会話継続（ルートループ用） | **`llm --cid "$ROOT_CID" -d "$ROOT_DB" "次は..."`**（`-c` は使わない） | conversation を **明示 ID 固定**（§6.1, [P0]） |
| 観測 | `llm logs list -d "$ROOT_DB" -n 50 --json` / `llm logs path` | **全呼び出しが SQLite に自動記録** |
| 検索的分解（任意） | `llm embed`/`llm similar` | 埋め込み類似で「manual decomposition」を強化 |

**キー設定（既定・v0.4）**: 余計な向き先設定は不要。**起動時に env で予算上限付き専用キーを渡す**だけで、`llm` がプロバイダへ直接つながる。

```bash
# run 起動時（ホスト）
docker run --rm -e OPENAI_API_KEY="$RLMSH_KEY" \
  --pids-limit 512 --memory 4g --cpus 2 \
  -v "$WORK_HOST:/work" \
  -v "$CONTEXT_HOST:/context:ro" \
  rlm-sandbox tail -f /dev/null
# $RLMSH_KEY = OpenAI project（予算ハード上限付き）の専用キー（§9）
```

- ルート用に `gpt-5`、サブ呼び出し用に `gpt-5-mini` を `llm -m <name>` で直接指定（プロバイダのモデル名そのまま）。`extra-openai-models.yaml` は**既定では不要**。
- `RLMSH_KEY` が未設定でも `OPENAI_API_KEY` へは自動フォールバックしない。広域キーを誤投入しないため、使う場合は明示的に `--api-key-env OPENAI_API_KEY` または `--allow-openai-key-fallback` を指定する。
- 任意（proxy を使う productionize 時のみ）: `extra-openai-models.yaml` の `api_base` を Host proxy に向け、`gw-gpt-5`/`gw-mini` のような proxy 経由モデル名にする（§5.4）。

### 5.4 LLM Proxy（任意・productionize 時のみ）

> **既定では実装しない。** §4.4 の通り、ソロ研究プロトタイプでは実キー直結 + プロバイダ予算上限で十分。以下は **sandbox を他人に開放する / 共有・CI で回す / 広域キーしか使えない / 第三者タスクを流す** ようになったときに有効化する選択肢。

導入すると得られるもの: ①実キーをサンドボックスから排除 ②モデル allowlist ③**run 単位**のコスト上限（プロバイダ予算は account 単位なので、run 粒度が欲しいとき）④相関 ID 付き集中ログ。

実装方針（採るなら）:
- **自作ミニ proxy（推奨）**: FastAPI/Flask + httpx の ~150 行。**OpenAI 互換リバースプロキシ**として作る — `llm` は OpenAI の HTTP ワイヤ仕様を喋るので、docker_repl.py の独自 JSON（`/llm_query`）ではなく `POST /v1/chat/completions`・`/v1/embeddings`・`GET /health` を実装し、実キーを付けて upstream へ中継（SSE 中継 or `stream:false` 正規化）。モデルは allowlist で写像（`gw-gpt-5`→`gpt-5` 等）。
- **代替**: [LiteLLM Proxy](https://github.com/BerriAI/litellm)（②③④を既製で持つ）。
- **キー非配置 + run 識別の両立**: sandbox には **run ごとの使い捨て仮想キー `sk-rlmsh-<run_id>`**（proxy 外では無価値）を起動時注入。proxy が `Authorization` から `run_id`・予算枠を引く。実キーはホストのみ。
- **ネットワーク**: 専用 `--internal` network + Gateway sidecar で「Gateway だけ到達可」に絞る（§5.1 の任意強化）。`llm` の `api_base` を `http://gateway:4000/v1` に向ける。
- 配置: `host/gateway.py`（proxy + §7 の Orchestrator 同居）。

> 補足: `rlm-sh` 再帰の **Sandbox Orchestrator（子 sandbox 生成・depth 計数, §7）は proxy とは独立**に必要。proxy を入れない既定構成でも Orchestrator は `host/` に置く。

### 5.5 Root Controller（差し替え可能アダプタ）

サンドボックス内 bash を駆動する「脳」。共通インタフェースは **「bash コマンド文字列を sandbox で実行し、(stdout, stderr, exit) を受け取る」** だけ。

#### A) pure-shell ループ（**デフォルト/リファレンス**）

ユーザ選択「完全シェル」。ルートループ自体を `llm` の会話で回す、最も RLM 論文に忠実かつ「LLM agent on bash」の主張に忠実な構成。

**実行場所の固定（[P1] 修正）**: MVP では実行モデルを一つに固定する —
> **ルートループはホストで走る `loop_shell.sh`**。**sandbox は永続コンテナ**（`tail -f /dev/null`）で、コマンドは `docker exec` で投入。**`/work` はホスト tmpdir をマウント**しているので、`answer.txt` 検出・ファイル監査はホスト側 path（`$WORK_HOST=$(mktemp -d)`）で行う。`run_in_sandbox` = 「`docker exec <cid> bash -lc <cmd>`」。

**会話の固定（[P0] 修正）**: `llm -c`（＝最新 conversation 継続）は、MAP 用の `llm` 呼び出しや別プロセスの `llm` が走ると **root loop が誤って別会話を継続し得る**。`llm 0.31` には `--cid` があるので、**初回応答後に `conversation_id` を取得し、以後は必ず `llm --cid "$ROOT_CID"` を使う**。さらに **root 用と subcall 用で `-d`（DB）を分離**し、混線と観測の取り違えを防ぐ。

```bash
# host/loop_shell.sh （ルートループのスケッチ。ホストで実行、sandbox は docker exec）
SYS=$(cat conf/system_prompt.md)
RUN_ID="run_$(date +%s)_$$"                 # 相関 ID（§10.2）
ROOT_DB="$RUN_DIR/root.db"                   # root 会話専用 DB（subcall とは別）
WORK_HOST="$RUN_DIR/work"                    # docker -v "$WORK_HOST:/work" でマウント済み

# 初手: システムプロンプト + クエリで会話開始。--no-log は付けず DB に必ず記録。
llm -m gpt-5 -d "$ROOT_DB" -s "$SYS" "Query: $QUERY" > /tmp/reply.txt
# 直後に conversation_id を取得して固定（以後はこの ID だけを継続）
ROOT_CID=$(llm logs list -d "$ROOT_DB" -n 1 --json | jq -r '.[0].conversation_id')
REPLY=$(cat /tmp/reply.txt)

for i in $(seq 1 "$MAX_ITERS"); do
  CMD=$(extract_bash_block "$REPLY")        # ```bash ...``` を抽出（rlm-minimal の find_code_blocks 相当）
  if [ -n "$CMD" ]; then
     OUT=$(run_in_sandbox "$CMD" 2>&1 | truncate_for_model)   # docker exec + §6.3 切り詰め
  fi
  # FINAL 検出はホスト側 path で（マウント済み /work）
  [ -s "$WORK_HOST/answer.txt" ] && { cat "$WORK_HOST/answer.txt"; break; }
  # ★ 常に --cid で root 会話を明示継続（-c は使わない）。subcall の llm は別 DB なので干渉しない。
  REPLY=$(llm --cid "$ROOT_CID" -d "$ROOT_DB" \
            "REPL output:\n$OUT\n\n次のアクション(\`\`\`bash\`\`\`)を。完了なら /work/answer.txt に書け。")
done
```

ポイント:
- **root の `llm` は `--cid "$ROOT_CID" -d "$ROOT_DB"` 固定**。サンドボックス内 MAP の `llm`（§5.3）は **別 DB（または無ログ）** にするので、root の会話を奪わない。
- root 会話・コスト・トークンは `ROOT_DB` に丸ごと残り、`llm logs list -d "$ROOT_DB" --json` で観測（§10.2）。
- `extract_bash_block` / `truncate_for_model` / `run_in_sandbox` はホスト側ヘルパ。

#### B) 外部エージェント CLI（Claude Code / Pi / codex）

「Pi cli や claude code cli から呼び出す形」への対応。2 方式:

- **B-1（推奨・素直）: エージェントをサンドボックス内で起動。** Claude Code/Pi をイメージに同梱し、`/work` 上で起動。エージェントの bash ツールは自然にコンテナ FS にスコープされ、`llm`・`rlm-sh` が bash から見える。エージェントのモデルアクセスも env のキー（proxy 使用時は proxy）経由に設定。→ **「Claude Code を rlm-sh サンドボックスに閉じ込めて、RLM 的挙動が出るか直接観察」できる**（RQ7、wiki の "CC can function as an RLM" の実証実験）。
- **B-2: エージェントはホストで動き、rlm-sh の bash 実行エンドポイントを叩く。** 外部エージェントが MCP/サブプロセスで `run_in_sandbox(cmd)` を呼ぶ。エージェント本体を改変せず差せるが、配線は増える。

#### C) 薄い Python ハーネス（任意）

ガードレール・バリデータ・停止条件を厳密に制御したい実験用。cheat-at-search の `harness(stoppers, validators, ...)` を移植。pure-shell が観測しづらいケースのフォールバック。

> **共通点:** どのアダプタでも sandbox / filesystem 規約は不変。脳だけ替わる。

---

## 6. 制御プロトコル（ルートループ）

### 6.1 ループ構造（rlm-minimal `completion()` 準拠）

参照: [`rlm-minimal/rlm/rlm_repl.py:76`](../../rlm-minimal/rlm/rlm_repl.py)

1. システムプロンプト + クエリで開始 → **初回応答直後に `conversation_id` を取得し `ROOT_CID` に固定**（[P0]）。
2. `max_iterations` 回まで（root の `llm` は毎回 `--cid "$ROOT_CID" -d "$ROOT_DB"`）:
   a. ルート LM が **bash コマンド**（` ```bash ` ブロック）を出す。
   b. sandbox で `docker exec` 実行、stdout/stderr を**切り詰めて**返す（§6.3）。
   c. ホスト側 `$WORK_HOST/answer.txt` が非空なら終了（FINAL 検出）。
3. 上限到達時は「これまでの情報で最終回答を」と最後の一押し（rlm-minimal と同じ安全弁）。

> **2 系統のログ DB（観測の前提）**: root の `llm` はホストで走り `ROOT_DB` に記録。サブ呼び出しの `llm` は**コンテナ内で走るので物理的に別の SQLite**（コンテナ内 `~/.config/io.datasette.llm/logs.db`、または `/work/.llm/subcalls.db` にマウント）に記録される。両者を `run_id` で突合する（§10.2）。この物理分離により `--cid` 混線は構造的にも起きない。

### 6.2 システムプロンプト設計

rlm-minimal の `REPL_SYSTEM_PROMPT`（[`prompts.py`](../../rlm-minimal/rlm/utils/prompts.py)）を bash 用に翻訳。要点:

- 環境の説明: 「あなたは bash を使える。`/context/` に巨大な入力がある。全部は一度に読めない。推測せず、まず `/context/` を調べる。」
- **無料の分解を促す**: 「まず `ls`/`wc`/`head`/`rg` で構造を把握せよ。LLM を呼ぶ前に grep/split で当たりを付けよ。」（RQ1 を促進）
- `llm` の使い方例（§5.3 の表をそのまま例示）と「サブ LLM は ~500K 文字入る。恐れず大きく渡せ」。
- MapReduce 戦略例: 「context を split → 各チャンクを `llm -s "抽出"` で処理し `buffers/` に保存 → 全 buffer を `llm` で集約 → `/work/answer.txt` に書く」。
- 終了規約: **`/work/answer.txt` に最終回答を書く**（§6.4）。
- 切り詰め注意: 「stdout は切り詰めて返される。大きな中間結果はファイルに書け（標準出力に垂れ流すな）」。

> ⚠️ **交絡注意（[P2]）**: 上記は分解を強く誘導するため、「自発的に分解した」証拠にはならない。H1/RQ1 を主張するときは §10.4 の **P-min / P-strategy / P-example の ablation** を必ず併用する。ここに書くのは P-example 条件のプロンプト。

### 6.3 切り詰め（truncation）戦略

RLM の「8192 char/`print`」に相当。bash は stdout が巨大化しやすいので重要（RQ5）。

- ルート LM に返すのは stdout の **先頭/末尾 N 行 + `wc` サマリ**（例: 先頭4KB + 末尾2KB + 「… (全 12,345 行, 3.2MB は /work/buffers/x に保存済)」）。
- 大出力は自動的にファイルへリダイレクト推奨をプロンプトで指示。
- stderr は別枠で短く（エラーは全文に近い方が有用なことが多い）。

### 6.4 最終回答規約（FINAL の bash 版）

- 第一候補: **`/work/answer.txt` の存在＋非空**で終了検出（filesystem-native、最も bash らしい）。
- 補助: `submit` という同梱コマンド（`submit <<<"answer"` または `submit -f buffers/final.txt`）。内部的に `answer.txt` を書く。`FINAL_VAR(var)` 相当 = `submit -f <file>`。
- ルートループは毎ターン `answer.txt` をチェック。

### 6.5 停止・検証（cheat-at-search の harness 移植・任意）

[`7_cheat_at_search_rlm_patents.py:122`](../../cheat-at-search-rlm/7_cheat_at_search_rlm_patents.py) の `harness()` 概念を採用可能:

- **stoppers**: `answer.txt` 検出 / `max_iterations` / `llm` 呼び出し回数上限 / 支出上限到達。
- **validators**: 回答フォーマット検証、`context` 改ざん検証、`answer.txt` が context 由来か簡易チェック。不合格なら是正メッセージを注入して継続（カウンタはループ/Orchestrator 側で持つ）。

---

## 7. 再帰（depth）設計

RLM v3 の主要知見「depth>1 が情報密度の高いタスク(OOLONG-Pairs)を大きく改善」（wiki: depth=3 で 76.0% vs depth=1 の 58.0%）を bash で観察する（RQ6）。

- **`rlm-sh` をコンテナ内コマンドとして提供**。`rlm-sh "サブクエリ" --context buffers/section_3.txt` をモデルが bash から呼べる。これが `rlm_query`（depth+1）。
- **nested Docker を避ける**: コンテナ内で更に Docker を起動するのは厄介。よって **コンテナ内 `rlm-sh` は thin client** とし、`{run_id, parent_sandbox_id, rel_context_path, query, depth}` を **Host の Sandbox Orchestrator に渡して子サンドボックス生成を依頼**する。「子環境生成という特権操作はホスト経由」というパターン（proxy の有無とは独立）。
  - 受け渡し方式（既定・最も素朴）: ホストは既に `/work` をマウントしているので、`rlm-sh` が **`/work/.spawn/<uuid>.json` に依頼を書き込み → Orchestrator が検知して子を起動 → 結果を `/work/.spawn/<uuid>.out` に返す**（ファイル＝インタフェース、ネットワーク不要）。HTTP エンドポイントにしてもよい。

#### 7.1 `/spawn` の権限境界契約（[P1] 修正）

`context_path` を**任意文字列として信頼しない**。path traversal / symlink / 親 `/work` 外参照 / TOCTOU を以下の契約で封じる:

1. **入力は相対パスのみ**: thin client は `rel_context_path`（親ワークスペース `/work` 起点の相対パス）と `run_id` だけを送る。絶対パス・`..`・先頭 `/` は Orchestrator が即拒否。
2. **realpath containment 検証（ホスト側）**: Orchestrator は親ワークスペースの**ホスト実体パス**（`$WORK_HOST`、`run_id` で引く）に `rel_context_path` を結合し、`realpath` で正規化したうえで **`$WORK_HOST` 配下に収まること**を検証（symlink を解決した後の prefix チェック）。外なら拒否。
3. **snapshot して渡す（TOCTOU 回避）**: 検証通過後、その時点のファイルを**子ワークスペースへコピー（snapshot）**してから子 sandbox を起動する。子は親 `/work` を共有せず、自分の `/context/` に複製を持つ。検証時刻と使用時刻の競合（TOCTOU）を構造的に排除。
4. **`run_id` 越えの参照禁止**: Orchestrator は `run_id` ↔ `$WORK_HOST` の対応表を握り、他 run のワークスペースを `rel_context_path` から触れないようにする（sandbox は自分の `run_id` のものだけ）。

- **depth メータリング**: 各呼び出しに `RLM_SH_DEPTH` を伝播。Orchestrator が `max_depth` 超を拒否。`llm_query`（=`llm` 一発）は depth を増やさない単発呼び出し、`rlm-sh`（=サブ環境フル稼働）は depth+1、という RLM の区別（`llm_query` vs `rlm_query`）を踏襲。
- 子サンドボックスは独立した `/work`（snapshot 済み context）を持ち、最終回答（`answer.txt`）だけを親の bash に stdout で返す。→ **中間状態は親コンテキストに載らない**（RLM の core property「outputs not in the context of the main model」を filesystem＋プロセス境界で達成）。

```
depth=0   親 sandbox: bash で grep/split、llm（単発）で MAP/REDUCE        ← llm_query 相当
depth=1   親 bash から `rlm-sh "q" -c sec.txt` → 子 sandbox 1個            ← rlm_query 相当
depth=2   子 sandbox の中で更に `rlm-sh ...` → 孫 sandbox（Orchestrator が生成）
...       max_depth でホストが打ち切り
```

---

## 8. サンドボックス選定

ユーザ要件: Docker を第一候補に。代替を併記。

| 候補 | 分離度 | bash+FS の自然さ | macOS 適性 | 並列/depth スケール | コスト/手間 | 評価 |
|---|---|---|---|---|---|---|
| **Docker コンテナ** ★第一候補 | 中〜高（namespace/cgroup, `--read-only`, `--pids-limit`, 使い捨て） | ◎ 完全な Linux userland | ◎ Docker Desktop 標準 | △ ローカル資源依存 | 低（既存 docker_repl.py 踏襲） | **採用**。bash+FS が欲しい本件に最適。既定は通常 bridge でプロバイダ直結（§5.1）。egress 制御は productionize 時の任意強化 |
| **E2B** | 高（クラウド隔離 microVM 系） | ◎ | ◎（ローカル無依存） | ◎ fan-out 容易 | 中（外部依存・APIキー・レイテンシ） | depth>1 大規模 fan-out 実験時の代替。`rlm/` が既に対応 |
| **Modal / Daytona / Prime** | 高 | ◎ | ◎ | ◎ | 中 | 同上。`rlm/rlm/environments/` に実装あり、流用可 |
| **ローカル使い捨て dir（分離なし）** | 低（rm/ネットワーク任意） | ◎ | ◎ | ◎ | 最低 | **初期反復のみ** `RLM_SH_UNSAFE=1`。信頼マシン専用。MVP 立ち上げ高速化用 |
| macOS `sandbox-exec`(Seatbelt) | 中 | ○ | △（deprecated 気味・難解） | △ | 中 | 非推奨。Docker で十分 |
| bubblewrap / firejail | 中 | ◎ | ✗（Linux 専用、ホストが macOS） | ○ | 低 | Linux CI 上での軽量代替 |
| gVisor / Firecracker | 高 | ◎ | △ | ○ | 高 | 強隔離が要るとき。プロトには過剰 |

**結論:** **Docker を第一候補**として実装（`Dockerfile.sandbox` + docker_repl.py 流用）。`BaseEnv`/`NonIsolatedEnv` の抽象（[`base_env.py`](../../rlm/rlm/environments/base_env.py)）を真似て **環境を差し替え可能 IF** にし、E2B/local を後から足せるようにする。初期の反復速度のため `local-unsafe` モードも用意。

---

## 9. セキュリティ・state integrity・ガードレール

脅威モデルと姿勢は §4.4（proportionate）に従う。**守る相手はモデル生成 bash の“事故”（課金爆発・データ破壊）であって、外部攻撃者の流出ではない**。よって守りは「安く効く」ものに絞る。

### 9.1 既定の守り（M1 必須・安く効く2点）

- **① プロバイダ側 予算ハード上限**: `llm` に渡すキーは **専用 OpenAI project / Anthropic workspace の、月/週の $ ハード上限を張ったキー**にする。これで暴走ループ（RQ4）や万一の漏洩の被害が**その予算枠に限定**される。account 単位の global cap なので proxy 不要。
  - **キーは 2 系統あることに注意**: (a) **sandbox 内サブコール**は起動時 env で注入する `RLMSH_KEY`（＝予算上限キー）を使う。(b) **ホストで走る root ループ**（`loop_shell.sh` の root `llm`）は**ホスト側 `llm` 自身に設定されたキー**を使い、`RLMSH_KEY` の予算枠は通らない。したがって **root 側のホストキーにも同様にプロバイダ予算上限を張る**こと。root 呼び出し回数は `MAX_ROOT_CALLS`（既定 16）で上限がかかるが、$ 上限はキー側で担保する。
- **② Docker 使い捨て + リソース制限**: `--rm`、writable `/work` と read-only `/context` のみマウント（他のホスト FS を触らせない）、`--pids-limit`（fork bomb 対策）、`--memory`/`--cpus`、`docker exec` に `timeout`。誤った `rm -rf` も `/work` 内に限定され run 後に破棄。
- **③ run ごとの簡易上限（任意・薄く）**: ルートループ/`rlm-sh` 側で **`llm` 呼び出し回数の上限カウンタ**と max_iterations を持つ（proxy 無しでもループ側で数えられる）。①の予算上限と二重化。

### 9.2 state integrity（RLM の "agents mutate/delete state" 問題の bash 版）

wiki §Limitations 8 / Turnbull の `repl.set()` 強制リセットに相当:

- **`/context/` は read-only マウント**（`:ro`）。モデルが入力を破壊できない（最も安く確実）。ハッシュ照合・復元は M4 の任意強化。
- **`answer.txt` の削除/上書きは許容**（書き直し前提）。`context` 破壊だけ構造的に阻止。
- **並列度の自制**: システムプロンプトで `xargs -P` の上限を指示（暴走並列の第一防壁。厳密強制が要れば②の `--pids-limit` が効く）。

### 9.3 既定で“やらないこと”（過剰・productionize 時に §5.4 で）

実キーのサンドボックス排除 / 自作 proxy / egress allowlist / per-run 仮想キー / ログのキースクラブ。これらは**外部流出・マルチテナント隔離**のための防御で、ソロ研究では実害が小さく、導入コスト（特に network 周りの摩擦）に見合わない。§4.4 の線引きに達したら有効化。

> 注: `llm` のキーは平文保存・コンテナ内 bash から読める。だから「キーをコンテナに置く＝モデルが読める」は受容する前提（=低価値・予算上限キーにする理由）。完全な漏洩防止は §5.4 の proxy 構成でしか得られないが、研究用途では①で被害を抑える方が比例的。

### 9.4 validators（任意・cheat-at-search 流）

回答が `context` に根拠を持つかの簡易検証、ハルシネーション検出。M4 で。

---

## 10. 観察したい挙動と評価

定性観察が主目的。`llm logs`（root + 各 sandbox の SQLite）+ `/work` のファイル痕跡で**トラジェクトリを丸ごと再現・分析**できるのが本構成の強み（proxy 有時は Gateway ログも）。

### 10.1 タスク

1. **NIAH（Needle in a Haystack）** — rlm-minimal の `main.py` を移植。100万行テキストに magic number を埋め、bash で見つけられるか。**期待挙動: `grep "magic number" context.txt` 一発**（→ RQ1: モデルは `llm` を呼ばず grep で解くか？ これが「無料の分解」の最初の観察）。
2. **patent expert finding** — cheat-at-search の課題を移植。`patent_search` を `/work/bin/patent_search` スクリプト化、`history.md` を可変メモリに、`llm` で関連性判定。エージェント的探索＋メモリ管理を観察。
3. **OOLONG / OOLONG-Pairs 風** — 情報密度が高く depth が効くタスク。depth=0/1/2 で精度・コスト・レイテンシを比較（RQ6, RLM v3 の再現）。
4. **長文 QA / 要約** — MapReduce（split→`llm`→集約）が自発的に出るか。

### 10.2 メトリクス

- 正答率（タスク依存）
- **`llm` 呼び出し回数 / 総トークン / 総コスト**（`llm logs --json` から集計）
- レイテンシ（RLM は単発比 +40〜80%。bash オーバーヘッドは？）
- **「無料の分解」比率**: grep/awk/split 等の非 LLM コマンド数 vs `llm` 呼び出し数（RQ1 の定量化）
- depth 別の精度・コスト（RQ6）
- 失敗分類: クオート崩れ（RQ2）、state 破壊（RQ3）、コスト超過（RQ4）、切り詰め起因の誤り（RQ5）
- ルート脳別比較（pure-shell vs Claude Code vs Pi、RQ7）

#### 相関 ID 設計（[P2] 観測の突合）

`llm logs --json` は `id`・`conversation_id`・`input_tokens`/`output_tokens`・`duration_ms`・`model` を返す（検証済み）が、**run / depth / どの bash コマンド由来か**は持たない。既定では突合元は **2 系統 + snapshot**（**`llm logs`（root DB + 各 sandbox DB）/ `/work` ファイル snapshot**）。proxy を入れた場合のみ Gateway ログが 3 系統目に加わる。共通の相関 ID で突合する:

| ID | 意味 | 伝播方法 |
|---|---|---|
| `run_id` | 1 回の rlm-sh 実行全体 | env で sandbox に注入、`/spawn` body、root DB のパス名（proxy 有時はヘッダにも） |
| `depth` | 再帰深さ（0,1,2…） | `RLM_SH_DEPTH` env、`/spawn` body |
| `sandbox_id` | コンテナ識別 | コンテナ起動時に採番、env |
| `parent_call_id` | 親 `llm`/`rlm-sh` 呼び出しの `id` | `/spawn`・サブ `llm` 呼び出しに伝播 |
| `command_index` | root ループの何ターン目の bash か | ループ側で採番、その bash 内の `llm` 呼び出しに env で渡す |
| `conversation_id` | `llm` の会話（root は `ROOT_CID`） | `llm logs --json` から取得 |

- **`llm logs`（既定の主軸）**: `id`/`conversation_id`/tokens/$ を持つ。`run_id`+`command_index`+`depth` は **DB パス命名規約**（root 用 `root.db`、各 sandbox 用 `sub_d<depth>_<sandbox_id>.db`）と **呼び出し時 env の外付け対応表**で紐付け、ホスト側コレクタが JOIN。root はホスト、subcall は各コンテナの DB なので回収（マウント or `docker cp`）。
- **`/work` snapshot（既定）**: 各ターン後に `$WORK_HOST` を `run_id/command_index/` 配下にスナップショットし、ファイル差分をトラジェクトリ（§10.3）と整列。
- **Gateway ログ（proxy 有時のみ・§5.4）**: proxy を入れる場合、`run_id` を per-run 仮想キー `sk-rlmsh-<run_id>` から導出して proxy 側で集中記録。既定構成では不要。

これで「どの run の depth=1 の 3 ターン目の bash が発行した MAP 呼び出しが何トークン・何ドルか」を一意に辿れる。

### 10.3 トラジェクトリ分析

RLM v3 §5 の error analysis（first decomposition の質、syntax error 率）に倣い、**最初の bash コマンド（first decomposition）の質**を分類。`llm logs` の会話 + `/work` のファイル差分を時系列で並べて観察。

### 10.4 プロンプト ablation（[P2] 自発性の交絡対策）

§6.2 のシステムプロンプトは grep/split/MapReduce を**かなり具体的に誘導**している。これは「うまく動くか」の観察には有効だが、**「モデルが自発的に分解した」証拠としては弱い**（誘導との交絡）。H1/RQ1 を正しく主張するため、同一タスク・同一モデルで以下 3 条件を比較する:

| 条件 | システムプロンプトの中身 | 測るもの |
|---|---|---|
| **P-min（最小）** | 「bash が使える。`/context/` に入力。`llm` でサブ呼び出し可。`/work/answer.txt` に答えを書け」だけ。戦略は一切示さない | **真の自発性**。grep/split/MapReduce が出れば H1 の強い証拠 |
| **P-strategy（戦略）** | P-min + 「LLM を呼ぶ前に grep/split で当たりを付けよ」等の**抽象的指針**（具体コマンド例は無し） | 指針だけで分解が誘発されるか |
| **P-example（例示）** | §6.2 フル（具体的な MapReduce コマンド例まで） | 例示でどれだけ底上げされるか（RLM 論文の「in-context examples が初期分解を改善」の再現） |

- 比較指標: §10.2 の「無料の分解比率」「正答率」「コスト」を 3 条件で。
- 期待: RLM 論文同様、**例示は精度を上げるが、自発性の証明には P-min の結果が要る**。P-min で分解が出なければ「bash 版 RLM はプロンプト依存」という重要な知見。
- **実行方法（実装済み）**: `conf/system_prompt.{min,strategy}.md` と既定 `conf/system_prompt.md`（example）を `loop_shell.sh --system-prompt <path>` で差し替え、各 run を `tasks/metrics.py --run-dir` で比較（README「検証したい挙動の叩き方」参照）。

---

## 11. リポジトリ構成と実装計画

### 11.1 構成（案）

（✓ = M0/M1 で実装済み・実機検証済み、○ = 一部、— = 未着手）

```
rlm-sh/
  docs/
    design.md                 # 本書
  Dockerfile.sandbox          # ✓ bash + llm + rlm-sh 同梱イメージ
  conf/
    system_prompt.md          # ✓ ルート用システムプロンプト（P-example 段・既定）
    system_prompt.strategy.md # ✓ P-strategy（抽象指針のみ・§10.4 ablation）
    system_prompt.min.md      # ✓ P-min（戦略なし・§10.4 ablation）
  bin/
    rlm-sh                    # ✓ サンドボックス内 thin client（再帰 spawn を Host に依頼）
    submit                    # ✓ answer.txt 書き込み（FINAL）
  host/
    sandbox.py                # ✓ Docker build/start/exec/cleanup + mount preflight
    loop_shell.sh             # ✓ ルートループ A: pure-shell（--cid 固定/--system-prompt/ホスト実行）
    loop_utils.py             # ✓ extract-bash / truncate / cid 抽出
    orchestrator.py           # — 子sandbox spawn・depth(§7)（M3）
    env_base.py               # — BaseEnv 抽象（Docker/E2B/local 差し替え）（M6）
    harness.py                # — ルートループ C: 薄い Python ハーネス（任意）
  tasks/
    niah.py                   # ✓ NIAH 生成（rlm-minimal/main.py 移植）
    metrics.py                # ✓ run の RQ1 集計（free 分解 vs llm 呼び出し）
    patents/                  # — cheat-at-search 移植（M-後）
  README.md

  # 任意（productionize 時のみ・§5.4）:
  #   host/gateway.py            # 自作 OpenAI 互換 proxy
  #   conf/extra-openai-models.yaml  # llm → proxy 向き先
  #   conf/models.allowlist.yaml     # proxy のモデル写像 + run 予算上限
```

### 11.2 マイルストーン（MVP ファースト）

- **M0 — sandbox 疎通（シンプル・v0.4）**: `Dockerfile.sandbox` をビルド → `docker run -e OPENAI_API_KEY=$RLMSH_KEY ...` で起動 → コンテナ内の `llm -m gpt-5-mini "hello"` がプロバイダに直接通る。
  - **完了条件**: ① sandbox から `llm -m gpt-5-mini "ok"` 成功、② `$RLMSH_KEY` が**予算ハード上限付きの専用キー**であること（プロバイダ管理画面で確認）、③ root 用 `llm` の `conversation_id` を `--cid` で 2 ターン継続できる、④ `llm`(コンテナ内) と root(ホスト) の DB が分離されている（DB パス命名規約）。
  - ここで「`llm` がカスタムヘッダを送れるか」も一応確認（将来 proxy を入れる場合の判断材料・必須ではない）。
- **M1 — pure-shell ループ + NIAH（最低限ガードレール込み）**: `host/loop_shell.sh` で **`--cid` 固定**の root ループ。Docker sandbox で bash 実行・切り詰め・`answer.txt` 検出（ホスト path）。NIAH を解かせ RQ1（grep で解くか）を観察。
  - **M1 から必須のガードレール（§9.1）**（後回しにしない・ただし安いものだけ）: **① 予算上限付き専用キー**、**② `--rm` + writable `/work` + read-only `/context` + `--pids-limit`/`--memory`/`--cpus` + `docker exec` の `timeout`**、mount プリフライト、ループ側の `llm` 呼び出し回数上限。proxy/egress allowlist は不要。
- **M2 — MapReduce 観察**: 長文 QA で split→`llm`→集約が自発的に出るか。切り詰め戦略の調整（RQ5）。subcall `llm` は root と別 DB。
- **M3 — 再帰（depth）**: `rlm-sh` thin client + Host Orchestrator の `/spawn`（**§7.1 の path containment + snapshot 契約を実装**）。depth メータリング。OOLONG 風で depth 比較（RQ6）。
- **M4 — 高度なガードレール / 観測強化**: context ハッシュ照合・復元、validators（回答の context 根拠検証）、相関 ID の全系統突合（§10.2）、支出ダッシュボード。
- **M5 — 外部脳アダプタ**: Claude Code / Pi をサンドボックス内ルートに（B-1）。RQ7 の比較実験。
- **M6 — 環境差し替え**: `env_base.py` 経由で E2B/local を選択可能に。

各マイルストーンは独立に観察価値があり、M1 だけでも H1 の最初の証拠が得られる。**最低限の隔離・コスト上限は M1 の完了条件に含む**（モデル生成 bash を無防備に走らせない）。

---

## 12. リスクと未解決問題

- **クオート/エスケープ（RQ2）**: 特殊文字・改行・巨大プロンプトを `llm "..."` に渡すと壊れやすい。緩和: 「プロンプトはファイルに書いて `llm < file` か `-f file`」をプロンプトで強制。観察対象でもある。
- **stdout 切り詰めの匙加減（RQ5）**: 強すぎると情報欠落、弱すぎるとコンテキスト腐敗。タスク別チューニングが要る。
- **再帰のコスト/レイテンシ爆発**: depth×fan-out が乗算で効く。**プロバイダ側の予算ハード上限 + ループ/Orchestrator 側の呼び出し回数・depth 上限**が頼り（§9.1）。
- **Docker on macOS のオーバーヘッド**: `docker exec` 毎回のレイテンシ。永続コンテナ（`tail -f /dev/null`）で軽減（docker_repl.py 同様）。
- **Docker bind mount の環境差**: Docker Desktop/rootless の file sharing/UID 設定によって `/tmp` や `/Users` bind mount が不可/readonly になる場合がある。既定 run dir は `rlm-sh/.runs/`、context は nested mount を避けた `/context:ro`、起動プリフライトで `/work` writable と `/context` read-only を即時検出する。
- **sandbox 内 `llm` サブコールの精密メータリングは未実装**: M1 の `MAX_ROOT_CALLS` は root loop 呼び出し上限であり、1つの bash 内の `llm` fan-out はプロバイダ予算上限・`docker exec` timeout・`--pids-limit` が主な歯止め。run 単位の厳密上限が必要になったら §5.4 proxy か in-container wrapper を追加する。
- **観測の完全性**: 既定のログは 2 系統（root `llm` DB / 各 sandbox `llm` DB）+ `/work` snapshot（proxy 有時に Gateway ログが 3 系統目）。`run_id` 等の相関 ID で突合（§10.2）。突合パイプラインを M0〜M4 で段階確立。
- **「bash だから」の交絡**: 精度差が「bash vs Python」由来か「モデルの bash 習熟」由来か切り分け困難。pure-shell vs Claude Code の比較（RQ7）で一部分離。
- **自発性の交絡（[P2] 対応済）**: §6.2 の誘導プロンプトと「自発的分解」を切り分けるため §10.4 の ablation（P-min/P-strategy/P-example）を必須化。
- **~~未決: `llm -c` の会話維持~~（[P0] 解決済）**: `llm 0.31` の `--cid` で root 会話を明示固定し、root と subcall で DB を分離（§5.5-A, §6.1）。`-c`（最新会話継続）は混線するため**使わない**方針に確定。M0 完了条件③④で「`--cid` の 2 ターン継続」と「root/subcall の DB 分離」を実機確認する。

---

## 13. 付録: 例セッション

NIAH を pure-shell ルートで解く想定トレース（理想挙動 / RQ1 が肯定される場合）:

```
[iter 1] root LM (gpt-5):
  ```bash
  ls -la /context/ && wc -l /context/context.txt
  ```
  → stdout: context.txt  1000000 lines  (切り詰め返却)

[iter 2] root LM:
  ```bash
  rg -n "magic number" /context/context.txt | head
  ```
  → stdout: 500123:The magic number is 4839201

[iter 3] root LM:
  ```bash
  echo "4839201" > /work/answer.txt
  ```
  → answer.txt 非空 → 終了。LLM サブ呼び出し 0 回（grep で解決）。
```

MapReduce が要る長文 QA の想定トレース（理想挙動）:

```
[iter 1] ```bash
  wc -c /context/*.md
  split -n l/20 /context/big.md /work/chunks/c_   # 20分割
  ```
[iter 2] ```bash
  ls /work/chunks/* | xargs -P4 -I{} sh -c \
    'llm -m gpt-5-mini -s "Qに関係する事実だけ抽出" "$(cat {})" > /work/buffers/$(basename {}).out'
  ```
[iter 3] ```bash
  cat /work/buffers/*.out | llm -m gpt-5 -s "抽出を統合してQに答えよ" > /work/answer.txt
  ```
  → 終了。MAP=20 サブ呼び出し(安いモデル) + REDUCE=1。
```

この 2 つが**自発的に**出れば H1 は支持される。出なければ（全部 `llm` に丸投げ／grep を使わない／クオート崩れで止まる等）、それ自体が「bash 版 RLM の表現力の限界」の貴重な観察データとなる。

---

### 参考

- RLM 概念: `wiki show concepts/rlm-recursive-language-models`（本設計の §1, §7 の根拠）
- REPL 実装の写経元: [`rlm-minimal/rlm/repl.py`](../../rlm-minimal/rlm/repl.py), [`rlm/rlm/environments/docker_repl.py`](../../rlm/rlm/environments/docker_repl.py)
- 環境抽象: [`rlm/rlm/environments/base_env.py`](../../rlm/rlm/environments/base_env.py)
- harness/validators: [`cheat-at-search-rlm/7_cheat_at_search_rlm_patents.py`](../../cheat-at-search-rlm/7_cheat_at_search_rlm_patents.py)
- `llm` CLI: https://github.com/simonw/llm （`extra-openai-models.yaml` の `api_base` で自作 proxy に向ける）
- LLM Proxy 任意代替: https://github.com/BerriAI/litellm （自作 proxy が手狭になった場合）
- （proxy を採るなら）真似る OpenAI 互換 API: `POST /v1/chat/completions`, `/v1/embeddings`（§5.4）
