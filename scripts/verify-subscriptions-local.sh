#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 || $1 != /* || ( $# == 2 && $2 != --offline ) ]]; then
  printf 'Usage: bash %s /absolute/new-output-directory [--offline]\n' "$0" >&2
  exit 2
fi
if [[ -e $1 || -L $1 ]]; then
  printf 'Refusing existing output path: %s\n' "$1" >&2
  exit 2
fi
repo=$(cd "$(dirname "$0")/.." && pwd -P)
output=$(cd "$(dirname "$1")" && printf '%s/%s' "$PWD" "$(basename "$1")")
mkdir "$output"
mkdir "$output/logs" "$output/work"
proof="$repo/scripts/prove-subscriptions.py"
bootstrap=$(command -v python3)
if [[ ${2:-} == --offline ]]; then
  export UV_OFFLINE=true CARGO_NET_OFFLINE=true
fi
"$bootstrap" "$proof" init --repo "$repo" --output "$output"
finish() {
  local code=$?
  trap - EXIT
  "$bootstrap" "$proof" finish --repo "$repo" --output "$output" --exit-code "$code"
  exit "$code"
}
trap finish EXIT
run() {
  local label=$1 code
  shift
  printf '\n[%s]' "$label"
  printf ' %q' "$@"
  printf '\n'
  set +e
  "$@" 2>&1 | tee "$output/logs/$label.log"
  code=$?
  set -e
  "$bootstrap" "$proof" record --repo "$repo" --output "$output" \
    --label "$label" --exit-code "$code" -- "$@"
  return "$code"
}

cd "$repo"
export UV_PROJECT_ENVIRONMENT="$output/dev"
export RUFF_CACHE_DIR="$output/ruff-cache"
export HYPOTHESIS_STORAGE_DIRECTORY="$output/hypothesis"
export CARGO_NET_GIT_FETCH_WITH_CLI=true
unset PYTHONPATH PYTHONHOME CARGO_TARGET_DIR
run uv-version uv --version
run rust-version rustc --version
run cargo-version cargo --version
run dev-sync uv sync --frozen --group dev --no-editable
run ruff "$output/dev/bin/ruff" check src tests scripts
run cargo-build cargo build --locked --manifest-path graph-adapter/Cargo.toml
run cargo-test cargo test --locked --manifest-path graph-adapter/Cargo.toml
run cargo-clippy cargo clippy --locked --manifest-path graph-adapter/Cargo.toml \
  --all-targets -- -D warnings
run pytest env PYTHONPATH="$repo/src" "$output/dev/bin/python" -m pytest \
  -p no:cacheprovider --basetemp "$output/pytest-tmp" -q
run wheel-build uv build --wheel --out-dir "$output/dist"
wheels=("$output"/dist/*.whl)
[[ ${#wheels[@]} == 1 && -f ${wheels[0]} ]]
run prod-create uv venv --python "$output/dev/bin/python" "$output/prod"
run prod-lock uv export --frozen --no-dev --no-emit-project \
  --output-file "$output/runtime-requirements.txt"
run prod-dependencies uv pip install --python "$output/prod/bin/python" \
  --require-hashes -r "$output/runtime-requirements.txt"
run prod-wheel uv pip install --python "$output/prod/bin/python" --no-deps "${wheels[0]}"
cd "$output/work"
run installed-proof "$output/prod/bin/python" -I "$proof" prove --repo "$repo" \
  --output "$output" --cli "$output/prod/bin/pcfs" \
  --adapter "$repo/graph-adapter/target/debug/pcfs-graph-adapter"
printf '\nVerification evidence: %s/verification.json\n' "$output"
