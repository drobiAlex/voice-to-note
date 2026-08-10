#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export VTN_HOME="$PWD"

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 — install: $2" >&2; exit 1; }
}
need uv "curl -LsSf https://astral.sh/uv/install.sh | sh"

uv sync --quiet
uv run vtn setup

if [ $# -gt 0 ]; then
  exec uv run vtn "$@"
fi

echo
echo "  process a memo:   ./run.sh process path/to/memo.m4a"
echo "  list memos:       ./run.sh list"
echo "  show transcript:  ./run.sh show <id>"
