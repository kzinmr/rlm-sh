#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

QUERY=""
CONTEXT_DIR=""
RUN_DIR=""
RUN_ID=""
IMAGE="rlm-sh-sandbox:dev"
ROOT_MODEL="gpt-5"
MAX_ITERS=12
MAX_ROOT_CALLS=16
EXEC_TIMEOUT=30
TRUNCATE_CHARS=12000
API_KEY_ENV="RLMSH_KEY"
SYSTEM_PROMPT=""
BUILD_IMAGE=0
KEEP_CONTAINER=0
READ_ONLY_ROOT=0
ALLOW_OPENAI_KEY_FALLBACK=0
SKIP_PREFLIGHT=0

usage() {
  cat <<'USAGE'
usage:
  host/loop_shell.sh --query "..." --context-dir /path/to/context [options]

options:
  --build                    Build Dockerfile.sandbox before starting.
  --image NAME               Docker image name (default: rlm-sh-sandbox:dev).
  --root-model MODEL         Root controller model (default: gpt-5).
  --max-iters N              Maximum bash execution turns (default: 12).
  --max-root-calls N         Maximum host llm calls (default: 16).
  --exec-timeout SECONDS     Timeout per docker exec (default: 30).
  --truncate-chars N         Max command-output chars returned to root (default: 12000).
  --api-key-env NAME         Host env var holding the low-budget key (default: RLMSH_KEY).
  --system-prompt PATH       Root system prompt file (default: conf/system_prompt.md).
                             Swap this for the §10.4 ablation (min / strategy / example).
  --run-dir DIR              Directory for root.db, work/, transcript.md.
  --run-id ID                Correlation id. Defaults to timestamp + pid.
  --read-only-root           Run container with read-only root filesystem.
  --allow-openai-key-fallback
                             Use OPENAI_API_KEY if --api-key-env is unset.
  --skip-preflight           Skip /work writable and /context read-only checks.
  --keep-container           Do not remove the container on exit.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --query)
      QUERY="$2"
      shift 2
      ;;
    --context-dir)
      CONTEXT_DIR="$2"
      shift 2
      ;;
    --run-dir)
      RUN_DIR="$2"
      shift 2
      ;;
    --run-id)
      RUN_ID="$2"
      shift 2
      ;;
    --image)
      IMAGE="$2"
      shift 2
      ;;
    --root-model)
      ROOT_MODEL="$2"
      shift 2
      ;;
    --max-iters)
      MAX_ITERS="$2"
      shift 2
      ;;
    --max-root-calls)
      MAX_ROOT_CALLS="$2"
      shift 2
      ;;
    --exec-timeout)
      EXEC_TIMEOUT="$2"
      shift 2
      ;;
    --truncate-chars)
      TRUNCATE_CHARS="$2"
      shift 2
      ;;
    --api-key-env)
      API_KEY_ENV="$2"
      shift 2
      ;;
    --system-prompt)
      SYSTEM_PROMPT="$2"
      shift 2
      ;;
    --build)
      BUILD_IMAGE=1
      shift
      ;;
    --keep-container)
      KEEP_CONTAINER=1
      shift
      ;;
    --read-only-root)
      READ_ONLY_ROOT=1
      shift
      ;;
    --allow-openai-key-fallback)
      ALLOW_OPENAI_KEY_FALLBACK=1
      shift
      ;;
    --skip-preflight)
      SKIP_PREFLIGHT=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "loop_shell.sh: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$QUERY" ]]; then
  echo "loop_shell.sh: --query is required" >&2
  usage >&2
  exit 2
fi
if [[ -z "$CONTEXT_DIR" || ! -d "$CONTEXT_DIR" ]]; then
  echo "loop_shell.sh: --context-dir must point to an existing directory" >&2
  exit 2
fi
if ! command -v llm >/dev/null 2>&1; then
  echo "loop_shell.sh: host llm command is not installed or not on PATH" >&2
  exit 127
fi
if [[ -z "$SYSTEM_PROMPT" ]]; then
  SYSTEM_PROMPT="$PROJECT_DIR/conf/system_prompt.md"
fi
if [[ ! -f "$SYSTEM_PROMPT" ]]; then
  echo "loop_shell.sh: system prompt file not found: $SYSTEM_PROMPT" >&2
  exit 2
fi

if [[ -z "$RUN_ID" ]]; then
  RUN_ID="run_$(date -u +%Y%m%dT%H%M%SZ)_$$"
fi
if [[ -z "$RUN_DIR" ]]; then
  RUN_DIR="$PROJECT_DIR/.runs/$RUN_ID"
fi

WORK_HOST="$RUN_DIR/work"
ROOT_DB="$RUN_DIR/root.db"
TRANSCRIPT="$RUN_DIR/transcript.md"
mkdir -p "$WORK_HOST" "$RUN_DIR"

if [[ "$BUILD_IMAGE" -eq 1 ]]; then
  python3 "$SCRIPT_DIR/sandbox.py" build --image "$IMAGE"
fi

read_only_args=()
if [[ "$READ_ONLY_ROOT" -eq 1 ]]; then
  read_only_args=(--read-only-root)
fi
fallback_args=()
if [[ "$ALLOW_OPENAI_KEY_FALLBACK" -eq 1 ]]; then
  fallback_args=(--allow-openai-key-fallback)
fi
preflight_args=()
if [[ "$SKIP_PREFLIGHT" -eq 1 ]]; then
  preflight_args=(--skip-preflight)
fi

CONTAINER="$(
  python3 "$SCRIPT_DIR/sandbox.py" start \
    --image "$IMAGE" \
    --work-dir "$WORK_HOST" \
    --context-dir "$CONTEXT_DIR" \
    --api-key-env "$API_KEY_ENV" \
    --run-id "$RUN_ID" \
    "${fallback_args[@]+"${fallback_args[@]}"}" \
    "${preflight_args[@]+"${preflight_args[@]}"}" \
    "${read_only_args[@]+"${read_only_args[@]}"}"
)"

cleanup() {
  if [[ "$KEEP_CONTAINER" -eq 0 && -n "${CONTAINER:-}" ]]; then
    python3 "$SCRIPT_DIR/sandbox.py" stop --container "$CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

echo "rlm-sh: run_id=$RUN_ID" >&2
echo "rlm-sh: run_dir=$RUN_DIR" >&2
echo "rlm-sh: container=$CONTAINER" >&2

SYS="$(cat "$SYSTEM_PROMPT")"
echo "rlm-sh: system_prompt=$SYSTEM_PROMPT" >&2
root_calls=0

root_llm() {
  local out_file="$1"
  shift
  if (( root_calls >= MAX_ROOT_CALLS )); then
    echo "loop_shell.sh: max root llm calls reached ($MAX_ROOT_CALLS)" >&2
    exit 73
  fi
  root_calls=$((root_calls + 1))
  llm "$@" > "$out_file"
}

prompt_file="$RUN_DIR/prompt_0.md"
reply_file="$RUN_DIR/reply_0.md"
printf 'Query:\n%s\n' "$QUERY" > "$prompt_file"
root_llm "$reply_file" -m "$ROOT_MODEL" -d "$ROOT_DB" -s "$SYS" --no-stream < "$prompt_file"
ROOT_CID="$(llm logs list -d "$ROOT_DB" -n 1 --json | python3 "$SCRIPT_DIR/loop_utils.py" cid || true)"
if [[ -z "$ROOT_CID" ]]; then
  echo "loop_shell.sh: failed to capture ROOT_CID from $ROOT_DB after the first call." >&2
  echo "  Without it, root turns cannot be pinned with --cid and would fork into" >&2
  echo "  separate conversations ([P0]). Aborting instead of running with an empty --cid." >&2
  echo "  Check: 'llm logs list -d \"$ROOT_DB\" -n 1 --json' and that host llm logging is ON" >&2
  echo "  ('llm logs status'). The first root reply is saved at: $reply_file" >&2
  exit 75
fi
echo "rlm-sh: root_cid=$ROOT_CID" >&2

{
  printf '# rlm-sh transcript\n\n'
  printf '- run_id: `%s`\n' "$RUN_ID"
  printf '- root_model: `%s`\n' "$ROOT_MODEL"
  printf '- root_db: `%s`\n' "$ROOT_DB"
  printf '- context_dir: `%s`\n\n' "$CONTEXT_DIR"
} > "$TRANSCRIPT"

for iter in $(seq 1 "$MAX_ITERS"); do
  cmd_file="$RUN_DIR/iter_${iter}.sh"
  if python3 "$SCRIPT_DIR/loop_utils.py" extract-bash < "$reply_file" > "$cmd_file"; then
    raw_file="$RUN_DIR/iter_${iter}.raw.txt"
    if python3 "$SCRIPT_DIR/sandbox.py" exec \
        --container "$CONTAINER" \
        --timeout "$EXEC_TIMEOUT" \
        -- "$(cat "$cmd_file")" > "$raw_file" 2>&1; then
      exit_status=0
    else
      exit_status=$?
    fi
  else
    raw_file="$RUN_DIR/iter_${iter}.raw.txt"
    exit_status=2
    printf 'No fenced bash block found in root reply.\n' > "$raw_file"
    : > "$cmd_file"
  fi

  out_file="$RUN_DIR/iter_${iter}.out.txt"
  {
    printf 'exit_status=%s\n' "$exit_status"
    cat "$raw_file"
  } | python3 "$SCRIPT_DIR/loop_utils.py" truncate --max-chars "$TRUNCATE_CHARS" > "$out_file"

  {
    printf '## Iteration %s\n\n' "$iter"
    printf '### Root Reply\n\n'
    cat "$reply_file"
    printf '\n\n### Bash Command\n\n```bash\n'
    cat "$cmd_file"
    printf '```\n\n### Sandbox Output\n\n```text\n'
    cat "$out_file"
    printf '\n```\n\n'
  } >> "$TRANSCRIPT"

  if [[ -s "$WORK_HOST/answer.txt" ]]; then
    cat "$WORK_HOST/answer.txt"
    exit 0
  fi

  prompt_file="$RUN_DIR/prompt_${iter}.md"
  {
    printf 'REPL output from bash command %s:\n\n' "$iter"
    printf '```text\n'
    cat "$out_file"
    printf '\n```\n\n'
    printf 'If the task is complete, write the final answer to /work/answer.txt or call submit.\n'
    printf 'Otherwise respond with exactly one fenced bash block for the next action.\n'
  } > "$prompt_file"
  next_reply="$RUN_DIR/reply_${iter}.md"
  root_llm "$next_reply" --cid "$ROOT_CID" -d "$ROOT_DB" -m "$ROOT_MODEL" --no-stream < "$prompt_file"
  reply_file="$next_reply"
done

final_prompt="$RUN_DIR/prompt_final.md"
final_reply="$RUN_DIR/reply_final.md"
{
  printf 'Maximum iterations reached. Based on the transcript so far, make one final attempt.\n'
  printf 'Respond with exactly one fenced bash block that writes the best available final answer to /work/answer.txt.\n'
} > "$final_prompt"
root_llm "$final_reply" --cid "$ROOT_CID" -d "$ROOT_DB" -m "$ROOT_MODEL" --no-stream < "$final_prompt"

final_cmd="$RUN_DIR/final.sh"
if python3 "$SCRIPT_DIR/loop_utils.py" extract-bash < "$final_reply" > "$final_cmd"; then
  python3 "$SCRIPT_DIR/sandbox.py" exec \
    --container "$CONTAINER" \
    --timeout "$EXEC_TIMEOUT" \
    -- "$(cat "$final_cmd")" > "$RUN_DIR/final.raw.txt" 2>&1 || true
fi

if [[ -s "$WORK_HOST/answer.txt" ]]; then
  cat "$WORK_HOST/answer.txt"
  exit 0
fi

echo "loop_shell.sh: no answer.txt produced; transcript is $TRANSCRIPT" >&2
exit 1
