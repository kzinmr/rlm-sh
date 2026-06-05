# rlm-sh 設計書 — Bash & Filesystem 上で動作する RLM プロトタイプ

> 💡 **一行で言うと:**  
> RLM (Recursive Language Models) は「Python REPL + メモリ変数 + `llm_query()` 関数」で長文コンテキストを再帰処理しますが、`rlm-sh` はそれを **「Bash + ファイルシステム + `llm` CLI」** という開発者にお馴染みの環境で再現し、同様の協調動作や最適化が成立するかを観察するための実験用プロトタイプです。
>
> *LLM agent on REPL w/ variable & `llm_query`* ➡️ *LLM agent on bash w/ filesystem & `llm` CLI*

---

## 📌 ドキュメント情報
* **ステータス**: Draft **v0.4** (ソロ研究向けのセキュリティ簡素化を反映)
* **対象読者**: 本プロジェクトの実装者・実験者・レビュー担当者
* **一次資料**: 
  * RLM 論文: [arXiv:2512.24601](https://arxiv.org/abs/2512.24601)
  * [RLM ブログ解説](https://alexzhang13.github.io/blog/2025/rlm/)
  * [simonw/llm GitHub](https://github.com/simonw/llm)

---

## 🛠️ v0.4 でのセキュリティ方針見直しについて
本プロジェクトは**「ローカルマシン上で動かすソロ研究用のプロトタイプ」**です。そのため、当初検討していた複雑なキー隔離 Proxy やネットワーク制限 (Egress Allowlist) などの多層防御は過剰であると判断し、オプション扱いに降格しました。

既定では、以下のシンプルな 2 重の防御策を採用します。
1. **API プロバイダ側で予算制限 (ハードリミット) を設定した専用キーを使用する**
2. **使い捨ての Docker コンテナと標準的なリソース制限を使用する**

これにより、実装の摩擦を減らしつつ、万が一の暴走による課金被害を最小限に抑えます。

---

## 📋 目次
1. [背景と目的](#1-背景と目的)
2. [中核となる仮説とリサーチクエスチョン](#2-中核となる仮説とリサーチクエスチョン)
3. [RLM 概念の対応表 (REPL ➡️ Bash)](#3-rlm-概念の対応表-repl-️-bash)
4. [アーキテクチャ全体像](#4-アーキテクチャ全体像)
5. [コンポーネント詳細](#5-コンポーネント詳細)
6. [制御プロトコル (ルートループ)](#6-制御プロトコル-ルートループ)
7. [再帰 (depth) 設計](#7-再帰-depth-設計)
8. [サンドボックス環境の選定](#8-サンドボックス環境の選定)
9. [セキュリティとガードレール](#9-セキュリティとガードレール)
10. [観察対象と評価計画](#10-観察対象と評価計画)
11. [リポジトリ構成とマイルストーン](#11-リポジトリ構成とマイルストーン)
12. [リスクと未解決問題](#12-リスクと未解決問題)
13. [付録: 想定トレース例](#13-付録-想定トレース例)

---

## 1. 背景と目的

### 1.1 RLM (Recursive Language Models) とは
RLM は、従来の `llm.completion(prompt)` を `rlm.completion(prompt)` に置き換える推論パラダイムです。以下の 4 つの要素で構成されます。

1. **コンテキストのオフロード**: 巨大な入力データを REPL 環境内の `context` 変数へ逃がし、メインの LLM には全文を直接見せない。
2. **プログラム的分解 (CodeAct)**: LLM が Python コードを出力・実行し、データのスライスや検索を行う。
3. **再帰的サブコール**: 必要に応じて `llm_query()` を呼び出し、分割したタスクを別モデルに解かせる。
4. **最終集約**: ルートモデルが結果をまとめ、最終回答を得る。

> [!NOTE]
> RLM の共著者は、「Python REPL は具体化の一つに過ぎず、本質は **『LLM 呼び出しがコード内で行われ、その中間出力がメインモデルの文脈を圧迫しないシンボリック環境 (Symbolic Environment)』** である」と述べています。

### 1.2 本プロジェクトの狙い
Python REPL の代わりに、現実の開発者が日常的に使用する **Bash シェルとファイルシステム** を「シンボリック環境」として使用します。
エージェントに「Bash が実行でき、ファイルシステム経由で `llm` CLI が叩ける」ことだけを伝え、自発的にファイルの分割 (split/grep) や再帰呼び出しを組み合わせた MapReduce パイプラインを構築できるかを実験します。

### 1.3 非目標 (Non-goals)
* 本番運用に耐えうる高スループットな実行システムの開発。
* LLM の追加学習 (Post-training)。本プロジェクトでは既存モデルを用いたスキャフォールドの工夫に留めます。
* ベンチマーク SOTA の更新。定性的な挙動の観察と分析に集中します。

---

## 2. 中核となる仮説とリサーチクエスチョン

### 2.1 仮説
> **H1 (自発的 MapReduce 構築)**:  
> ルートモデルに対し、「Bash が利用可能で、`/context/` 配下にファイルがあり、`llm` や `rlm-sh` を叩くことで再帰的にタスクを処理できる」と伝えるだけで、モデルは自発的に「ファイルを `grep`/`split` 等で分割 ➡️ `llm` で並列前処理 ➡️ 結果を集計して回答」という RLM 的な MapReduce パイプラインを構築する。

### 2.2 Bash 環境ならではの利点

| 観点 | RLM (Python REPL) | rlm-sh (Bash + Filesystem) | メリット |
| :--- | :--- | :--- | :--- |
| **安価な分解** | `re.findall`, list スライス | `grep`, `rg`, `awk`, `sed`, `split`, `jq` | LLM を使わずに、高速かつ事実上無制限のテキスト検索や分割を行える。 |
| **メモリの永続性** | In-memory 変数 (`locals`) | ファイルシステム上のファイル | 中間データを永続化でき、人間からも読みやすく、`grep` 等で再検索可能。 |
| **観測性** | カスタム実装が必要 | `llm logs` (SQLite) | 全ての LLM 呼び出しログ、消費トークン、コストが自動的に SQLite に記録される。 |
| **パイプライン処理** | 関数の合成 | シェルのパイプ (`|`) | `cat file | llm -s "要約"` のように自然なストリーム処理が可能。 |
| **再帰処理** | `Sub_RLM` クラスの置換 | `rlm-sh` コマンドの呼び出し | 再帰的に子シェルを立ち上げるだけで、本物のプロセス分離と再帰が書ける。 |

### 2.3 リサーチクエスチョン (RQ)
* **RQ1 (自発的分解)**: モデルは安価な Bash コマンドを使ってデータを絞り込むか、あるいは何でも `llm` コマンドに丸投げしてトークンを浪費するか？
* **RQ2 (クオート崩れ)**: 特殊文字や改行を含むプロンプトを `llm "..."` のように渡した際、シェルのクオート規則によってエラーが発生しないか？
* **RQ3 (状態破壊)**: モデル自身の操作ミスによって、作業中のファイルやコンテキストファイルが上書き・削除されてしまわないか？
* **RQ4 (コスト爆発)**: バックグラウンド処理 (`&`) による並列 `llm` 呼び出しが暴走して fork bomb 化しないか？
* **RQ5 (出力の切り詰め)**: Bash の標準出力をルートモデルに返す際、情報損失やコンテキスト汚染を起こさずに切り詰める最適なバランスは何か？
* **RQ6 (再帰による精度変化)**: 再帰深さ (depth > 1) を許容することで、情報密度の高いタスクの解答精度が向上するか？
* **RQ7 (モデルごとの違い)**: 制御エンジンを Pure-shell とした時と、Claude Code などの高度なコーディングエージェントにした時で RLM 的挙動にどう差が出るか？

---

## 3. RLM 概念の対応表 (REPL ➡️ Bash)

| RLM (Python REPL) | rlm-sh (Bash + Filesystem) | 備考・対応方針 |
| :--- | :--- | :--- |
| **` ```repl ` の出力** | **` ```bash ` の出力** | モデルが実行したい Bash コマンドを出力する。 |
| **`exec()` を用いた REPL** | **Docker 内の Bash シェル** | 制御の核をシェルに移管。 |
| **`context` 変数** | **`/context/` 配下のファイル** | 巨大な入力ファイル群。read-only で安全にマウント。 |
| **`locals()` (作業用変数)** | **`/work/` 配下のファイル群** | モデルが自由に書き込み可能なワークスペース。 |
| **`llm_query(prompt)`** | **`llm "prompt"`** | `llm` CLI コマンドによる単発呼び出し。 |
| **手動バッチ処理** | `xargs -P` や `&` による並列実行 | シェルレベルでの並列化。 |
| **`Sub_RLM` / 再帰呼び出し** | **`rlm-sh "q" --context f`** | プロセスを分けて `rlm-sh` を再帰的に実行。 |
| **`FINAL(answer)`** | **`/work/answer.txt` への出力** | 回答用ファイルを書き終えたら終了と判定する。 |
| **出力の 8192 文字制限** | **標準出力の `head`/`wc` 切り詰め** | モデルに返すテキストのサイズ制限。ファイルへの書き込みは無制限。 |

---

## 4. アーキテクチャ全体像

`rlm-sh` は、ホスト側のオーケストレーターと、使い捨ての Docker サンドボックスコンテナで構成されます。

### 🏗️ システム連携図 (Mermaid)

```mermaid
graph TD
    subgraph Host [ホスト環境]
        Root[Root Controller<br/>loop_shell.sh 等]
        Orch[Sandbox Orchestrator<br/>orchestrator.py]
    end

    subgraph Sandbox [使い捨て Docker Sandbox]
        Bash[Bash & Unix 標準ツール<br/>grep / awk / split / jq]
        LLM[llm CLI]
        RLMSh[rlm-sh thin client]
        
        subgraph Filesystem [/work - 読み書き可能]
            Ans[answer.txt - 最終回答]
            Chunks[chunks / buffers - 中間メモリ]
        end
        
        subgraph Input [/context - 読み取り専用]
            Ctx[context.txt - 入力データ]
        end
    end

    Provider[LLM プロバイダ API<br/>api.openai.com 等]

    Root -->|docker exec| Bash
    Bash -->|ファイル操作| Filesystem
    Bash -->|読み取り| Input
    Bash -->|llm コマンド実行| LLM
    LLM -->|API リクエスト<br/>予算制限付き API キー| Provider
    Bash -->|再帰実行| RLMSh
    RLMSh -->|リクエストファイル出力| Filesystem
    Orch -->|ファイル監視 / 子コンテナ作成| Sandbox
```

### 4.1 基本構成
1. **Sandbox (Dockerコンテナ)**:
   [Dockerfile.sandbox](file:///Users/kazukiinamura/rlm/rlm-sh/Dockerfile.sandbox) からビルドされた使い捨ての環境。モデルの実行する Bash コマンドや `llm` CLI はここで処理されます。API キーはコンテナ起動時に環境変数として安全に注入され、プロバイダへ直接通信します。
2. **Host (ホストマシン)**:
   ルートループ ([host/loop_shell.sh](file:///Users/kazukiinamura/rlm/rlm-sh/host/loop_shell.sh)) はホスト側で動作し、`docker exec` 経由でサンドボックス内の Bash を駆動します。子コンテナの再帰的な生成や、時間切れ・リソース制限の管理もホスト側で行います。
3. **Root Controller (差し替え可能な脳)**:
   ホストからコンテナへ指示を投げるエージェントプログラム。シンプルな Shell ループのほか、MCP 等を介して Claude Code などのエージェントと接続することも可能です。

---

## 5. コンポーネント詳細

### 5.1 Sandbox 層
* **ベースイメージ**: `debian:bookworm-slim` をベースとし、`bash`, `ripgrep`, `jq`, `gawk`, `curl`, `python3` 等のデータ操作ツールをプリインストールします。
* **`llm` CLI (simonw/llm)**:
  コンテナ内に `llm` コマンドを配置し、API 呼び出しの抽象化レイヤーとして使用します。
* **マウントルール**:
  * `/context`: 入力データを読み取り専用 (`:ro`) でマウント。
  * `/work`: ホスト側のランダムテンポラリディレクトリを読み書き可能でマウント。
* **起動プリフライト**:
  コンテナ起動の直後、ホストから `/work` への書き込みテストと `/context` の読み取り専用制限の検証を行い、マウントの問題を事前に検知します。

### 5.2 ファイルシステムのフォルダ構造
システムプロンプトでモデルに提示されるワークスペースの標準構造です。

```
/context/          # 【読み取り専用】入力データ (例:巨大な1枚の txt や分割された md ファイル群)
  context.txt

/work/             # 【読み書き可能】モデルの「メモリ」として機能するフォルダ
  answer.txt       # ★ 最終回答用ファイル。ここに書き込まれた時点でタスク完了と見なす
  history.md       # モデルが自身の思考や手順を記録する永続ログ
  chunks/          # grep や split コマンドで分割された一時テキストの保存先
  buffers/         # llm のサブコールによる中間出力を保存するバッファ
  notes.md         # スクラッチパッド（雑記帳）
  bin/             # 必要に応じて追加されるドメイン固有のカスタムコマンド
```

### 5.3 `llm` CLI の活用例
`rlm-sh` において、`llm` コマンドは以下のような様々な形態で実行されます。

```bash
# 基本的な質問 (One-shot)
llm "Who wrote Romeo and Juliet?"

# 巨大テキスト (stdin) を流し込んでシステムプロンプトで指示
cat chunks/chunk_01.txt | llm -s "Summarize key points in bullet format."

# サブモデル (安価なモデル) を明示的に指定して実行
llm -m gpt-5-mini "Analyze this code."

# 大容量のテキストファイルをファイル参照で処理
llm -f /context/long_document.txt "Extract names of participants."

# バッチ処理 (並列実行) の例
ls chunks/* | xargs -P 4 -I {} sh -c 'llm -s "Extract data" < {} > buffers/$(basename {}).out'
```

* **会話の固定**: 
  ルートコントローラーでの対話継続には `-c` (最新会話の継続) フラグは使用せず、`llm --cid <ROOT_CID> -d <ROOT_DB>` のように**会話IDとデータベースを明示的に指定・分離**します。これにより、サブコールとして実行された `llm` コマンドとの会話の混線を完全に防ぎます。

### 5.4 LLM Proxy (任意・本番/マルチテナント運用向け)
個人によるソロ研究では不要なため、v0.4 ではオプションに降格となりましたが、**「サンドボックスに直接 API キーを置きたくない」「マルチテナントで細かく利用量制限やログ収集を行いたい」**場合には、ホスト側に OpenAI 互換の簡易リバースプロキシを立て、コンテナの `api_base` をそこへ向けます。

### 5.5 Root Controller

#### A) Pure-shell ループ (デフォルト)
ホスト側で実行される [host/loop_shell.sh](file:///Users/kazukiinamura/rlm/rlm-sh/host/loop_shell.sh) がルート脳となります。
1. ホスト側で `ROOT_DB` を生成し、初回プロンプトを投げて会話を開始。
2. レスポンスから `ROOT_CID` (会話 ID) を取得し、以後のターンはすべて `--cid "$ROOT_CID" -d "$ROOT_DB"` を指定して会話を維持。
3. レスポンスから ` ```bash ` ブロックを抽出し、`docker exec` で Sandbox 内で実行。
4. 出力を適度に切り詰めてルートモデルに返却。
5. ホスト側マウントの `/work/answer.txt` に回答が書き込まれるか、規定ターン数に達するまで繰り返す。

#### B) 外部エージェント CLI (Claude Code / Pi CLI 等)
エージェント自身をサンドボックスコンテナ内で起動し、コンテナ内の `/work` やツール群にアクセスさせます。これにより、既存の強力な自律型 CLI を RLM サンドボックス内に閉じ込めて行動を分析できます。

---

## 6. 制御プロトコル (ルートループ)

```
[開始] システムプロンプト + クエリ を入力
   │
   ▼ (ターン開始)
[1] ルート LLM に指示を送り、レスポンスを取得
   │
   ├─► レスポンスから `ROOT_CID` を固定 (初回のみ)
   │
   ▼
[2] レスポンスから ` ```bash ` ブロックを抽出
   │
   ├── (コマンドが存在しない、または answer.txt が生成された場合) ──► [終了]
   │
   ▼
[3] サンドボックス内でコマンドを実行 (タイムアウト管理あり)
   │
   ▼
[4] 標準出力を「先頭 N 行 + 末尾 M 行 + 行数/容量サマリ」に切り詰める
   │
   ▼
[5] 切り詰められた結果を LLM にフィードバックして [1] へ戻る (最大 Iteration 回数まで)
```

### 6.1 ルートループとサブコールの分離
ルートモデル用の `ROOT_DB` (ホスト側) と、コンテナ内から実行されるサブコール用のデータベース (コンテナ側) は物理的に分離されているため、会話データが混ざり合うことはありません。

### 6.2 システムプロンプトの役割
[conf/system_prompt.md](file:///Users/kazukiinamura/rlm/rlm-sh/conf/system_prompt.md) には、エージェントが効率的に動作するためのルールとヒントが含まれています。
* **無料の分解の推奨**: `grep` / `split` / `ripgrep` 等を使って、まず安価に入力データを絞り込むよう指示。
* **出力制限の説明**: 標準出力が大きすぎると自動で切り詰められるため、中間データは標準出力に流さず `/work/` 配下にファイルとして保存するよう警告。

### 6.3 出力切り詰めルール
モデルへ返される標準出力は、以下のルールで切り詰められます。
* **切り詰めフォーマット**: `[先頭 4KB]` + `\n... (中略: 全 XXX 行, YYY バイト) ...\n` + `[末尾 2KB]`
* **エラー出力 (stderr)**: デバッグに重要な情報が含まれるため、切り詰めず可能な限りそのまま返却します。

---

## 7. 再帰 (depth) 設計

RLM の最大の特徴である「サブタスク用の再帰的サンドボックス」を再現します。

1. モデルがコンテナ内で `rlm-sh "サブクエリ" --context chunks/c01.txt` を実行する。
2. コンテナ内の `rlm-sh` (薄いクライアント) は、ホスト側のオーケストレーター ([host/orchestrator.py](file:///Users/kazukiinamura/rlm/rlm-sh/host/orchestrator.py) 予定) に対し、子サンドボックスの起動を要求する。
   * **通信手段**: 最もシンプルなファイルベースのポーリング通信を採用。`/work/.spawn/<uuid>.json` に要求を書き込み、ホスト側が検知。
3. ホストは親コンテナの環境情報・深さ (depth) を引き継ぎつつ、独立したリソース制限を課した**子サンドボックスコンテナ**を起動。
4. 子コンテナでの処理完了後、最終結果 (`answer.txt`) のみを親の stdout に返却する。

### 7.1 安全な子コンテナ起動契約 (`/spawn` の脆弱性対策)
子コンテナ起動要求の処理において、ディレクトリトラバーサルやファイルの改ざん (TOCTOU脆弱性) を防ぐため、以下の契約を厳守します。

1. **相対パスの強制**: 指定できるコンテキストファイルは親ワークスペース `/work` を起点とする相対パスのみに限定。先頭の `/` や `..` を含むリクエストはホストが即時拒否。
2. **実体パス検証 (Path Containment)**: ホストは `realpath` でパスを正規化し、親ワークスペースのルートの外側を参照していないか検証。
3. **コピー＆隔離 (Snapshot)**: 共有マウントによる同時編集・改ざんを防ぐため、コンテキストファイルを一旦**子ワークスペースに物理コピー (Snapshot)** してから子サンドボックスを立ち上げる。
4. **親子関係の管理**: ホスト側ですべてのコンテナに `run_id` と `parent_sandbox_id` のラベルを付与し、親が停止した場合は子孫コンテナすべてをまとめて破棄する Reaper プロセスを用意する。

---

## 8. サンドボックス環境の選定

| 環境 | 分離度 | 使いやすさ | Mac適性 | スケール性 | 導入コスト | 総合評価 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Docker** (第一候補) | **中〜高** | **極めて良い** | **良い** (Desktop) | 中 | 低 | **採用。本プロジェクトの既定環境。** |
| **E2B** | 高 (MicroVM) | 良い | 良い | 高 (Cloud) | 中 | 外部サーバー依存となるが、大量並列が必要な場合の代替先。 |
| **Modal / Daytona** | 高 | 普通 | 普通 | 高 | 中 | E2B 同様、将来的な大規模実験用の予備。 |
| **ローカル一時フォルダ** | なし | 良い | 良い | 中 | なし | `RLM_SH_UNSAFE=1` で動作。素早い初期動作デバッグ専用。 |

---

## 9. セキュリティとガードレール

「モデルの悪意」ではなく「モデルの事故 (無限ループ、誤動作によるファイル消去)」からホスト環境や課金限界を守るための最小ガードレール設計です。

1. **プロバイダ側でのハード予算リミット**:
   コンテナ内で使用する `RLMSH_KEY` (API キー) は、プロバイダ側で週/月の上限額 (例: $10 など) を設定した専用キーとします。
   * ⚠️ **注意**: ルートコントローラー (ホスト側) のキーと、サンドボックス (コンテナ側) のキーの**両方に**予算上限を設定してください。
2. **サンドボックスリソース制限**:
   Docker 起動オプションで、最大 CPU 使用率、メモリ (4GB 制限)、`--pids-limit 512` (fork bomb 回避) を設定。
3. **`docker exec` タイムアウト**:
   モデルが実行した Bash がハングした場合に備え、ホスト側の `exec` コマンドに `timeout 60` を適用。
4. **入力の不変性 (State Integrity)**:
   `/context` ディレクトリを `:ro` (Read-Only) でマウントすることで、モデルによる入力ファイルの誤消去を物理的に防ぎます。

---

## 10. 観察対象と評価計画

### 10.1 テスクシナリオ
* **NIAH (Needle in a Haystack)**: 
  巨大なテキストから 1 つの数字を探索。モデルが `grep` 一発で解くか (無料の分解) を評価。
* **Patent Expert Finding**: 
  特許検索タスク。検索ツールと `history.md` による思考記録を用いたエージェント的振る舞いを観察。
* **MapReduce (長文要約)**: 
  分割・LLMマッピング・集約のプロセスが自発的に行われるか。
* **OOLONG-Pairs**: 
  情報密度の高いタスク。再帰深さの違いが最終精度にどう影響するか。

### 10.2 メトリクスの算出
* **`free_to_llm_ratio`**: `grep`等のコマンド回数 ÷ `llm`呼び出し回数。
* **エラーの分類**: クオート崩れ (RQ2)、破壊エラー (RQ3)、切り詰めによる誤認 (RQ5) などの発生頻度。
* **モデルごとの振る舞い比較**: 同一タスクでの Pure-shell vs Claude Code 等の挙動ログ比較。

### 10.3 ログの相関 ID 突合表
ホストとコンテナのデータベースを繋ぎ合わせるための共通相関 ID 設計です。

| ID名 | 意味・目的 | 伝播・記録方法 |
| :--- | :--- | :--- |
| **`run_id`** | 1 回の RLM 実行全体を識別する ID | ホストで採番、環境変数としてコンテナへ注入 |
| **`depth`** | 再帰の階層深さ (0, 1, 2...) | `RLM_SH_DEPTH` 環境変数および子コンテナ生成リクエストに含める |
| **`sandbox_id`** | 各コンテナの識別子 | コンテナ起動時に自動採番 |
| **`command_index`** | ルートループの実行ターン数 | ホストのループ制御カウンタ |
| **`conversation_id`** | LLM 会話セッション ID | `llm logs` の SQLite レコードから取得 |

---

## 11. リポジトリ構成とマイルストーン

### 11.1 リポジトリファイルマップ

* [Dockerfile.sandbox](file:///Users/kazukiinamura/rlm/rlm-sh/Dockerfile.sandbox) — サンドボックスイメージ定義
* **`conf/`** — 各種システムプロンプト
  * [system_prompt.md](file:///Users/kazukiinamura/rlm/rlm-sh/conf/system_prompt.md) — 既定プロンプト (例示あり)
  * [system_prompt.strategy.md](file:///Users/kazukiinamura/rlm/rlm-sh/conf/system_prompt.strategy.md) — 抽象方針のみ
  * [system_prompt.min.md](file:///Users/kazukiinamura/rlm/rlm-sh/conf/system_prompt.min.md) — 最小定義 (誘導なし)
* **`bin/`** — サンドボックス内ツール
  * [rlm-sh](file:///Users/kazukiinamura/rlm/rlm-sh/bin/rlm-sh) — 再帰要求用 thin client
  * [submit](file:///Users/kazukiinamura/rlm/rlm-sh/bin/submit) — 回答書き込みツール
* **`host/`** — ホスト側コントローラー
  * [sandbox.py](file:///Users/kazukiinamura/rlm/rlm-sh/host/sandbox.py) — Docker 管理用スクリプト
  * [loop_shell.sh](file:///Users/kazukiinamura/rlm/rlm-sh/host/loop_shell.sh) — ルートループスクリプト
  * [loop_utils.py](file:///Users/kazukiinamura/rlm/rlm-sh/host/loop_utils.py) — パーサー・切り詰めユーティリティ
  * `orchestrator.py` — 子サンドボックス管理 (M3予定)
* **`tasks/`** — テストと評価
  * [niah.py](file:///Users/kazukiinamura/rlm/rlm-sh/tasks/niah.py) — NIAH テストデータ生成
  * [metrics.py](file:///Users/kazukiinamura/rlm/rlm-sh/tasks/metrics.py) — 実行ログ解析

### 11.2 実装マイルストーン
* **M0 (コンテナ疎通確認)**: Sandbox ビルドおよびコンテナ内から `llm` コマンドで API が正常に叩けることの確認。
* **M1 (単一ルートループの確立)**: Docker 上での Bash 実行、標準出力の切り詰め、および NIAH テストでの `grep` 自発的実行の確認。
* **M2 (MapReduceの検証)**: 長文要約などのタスクにおいて、自発的な split と集計が行われるかを定量評価。
* **M3 (再帰機能の実装)**: `rlm-sh` クライアントとホスト側 Orchestrator の実装、および安全なパスチェック・Snapshotコピーによる再帰呼び出しの検証。
* **M4 (監視とガードレールの強化)**: コンテキストファイルの不変性バリデータの実装、および詳細なログ収集。
* **M5 (外部エージェント接続)**: サンドボックス内で Claude Code 等を動かし、挙動の差異を評価。
* **M6 (マルチ環境対応)**: `env_base` による E2B などへの差し替え対応。

---

## 12. リスクと未解決問題
1. **シェルのエスケープ崩れ (RQ2)**: 
   プロンプト内の特殊文字が原因で `llm "..."` が失敗するリスク。  
   * ➡️ **対策**: プロンプトを一度ファイルに吐き出し、`llm < file` または `llm -f file` で渡す手法をシステムプロンプトでモデルに学習させる。
2. **切り詰めによる迷子 (RQ5)**: 
   出力の切り詰めによって、モデルが以前の自分の出力を見失う問題。
   * ➡️ **対策**: `/work/history.md` に各ターンの重要な事実を明示的に記録させ、文脈を維持する。
3. **並列処理のフォーク爆弾化 (RQ4)**: 
   `xargs -P` の過剰な並列化や無限ループ。
   * ➡️ **対策**: Docker の `--pids-limit` とプロバイダ側予算制限を最後の砦とする。

---

## 13. 付録: 想定トレース例

### 🟢 パターンA. NIAH タスクでの理想的な挙動 (自発的分解の成功)
```
[Turn 1] ルート LLM:
  ```bash
  ls -la /context/ && wc -l /context/context.txt
  ```
  ホスト実行結果 ➡️ context.txt (100,000 行) が存在することを確認。

[Turn 2] ルート LLM:
  ```bash
  rg -n "magic number" /context/context.txt
  ```
  ホスト実行結果 ➡️ 54201: The magic number is 998822.

[Turn 3] ルート LLM:
  ```bash
  submit "998822"
  ```
  ホスト実行結果 ➡️ /work/answer.txt が作成され、ループが正常終了。 (LLM 呼び出し回数: 0回)
```

### 🔵 パターンB. 長文要約タスクでの自発的 MapReduce の挙動
```
[Turn 1] ルート LLM:
  ```bash
  split -n l/10 /context/huge_book.txt /work/chunks/c_
  ```
  ホスト実行結果 ➡️ ファイルを 10 個のチャンクに分割完了。

[Turn 2] ルート LLM:
  ```bash
  ls /work/chunks/* | xargs -P 4 -I {} sh -c \
    'llm -m gpt-5-mini -s "Extract core arguments" < {} > /work/buffers/$(basename {}).out'
  ```
  ホスト実行結果 ➡️ 4並列で安価なモデルを呼び出し、10個の抽出結果バッファを生成完了。

[Turn 3] ルート LLM:
  ```bash
  cat /work/buffers/*.out | llm -m gpt-5 -s "Summarize the extracted points" > /work/answer.txt
  ```
  ホスト実行結果 ➡️ 最終要約を answer.txt に書き込み、ループ終了。 (Map: 10回 [安いモデル]、Reduce: 1回 [賢いモデル])
```
