#!/usr/bin/env bash
# Install (or reinstall) the global `odin` uv-tool from this checkout.
#
# After pulling local changes:
#
#   ./scripts/install-tool.sh
#   ./scripts/install-tool.sh --editable   # live link; no reinstall needed for edits
#
# Requires `uv` on PATH. Uses --force so an existing install is replaced.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EDITABLE=0

usage() {
  cat <<'EOF'
Usage: scripts/install-tool.sh [--editable]

Install the global `odin` command from this repository via uv tool.

  --editable, -e   Install in editable mode (source edits apply without reinstall)
  -h, --help       Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -e|--editable) EDITABLE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "odin: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v uv >/dev/null 2>&1; then
  echo "odin: uv not found on PATH" >&2
  exit 1
fi

args=(tool install --from "$ROOT" --force)
if [[ "$EDITABLE" -eq 1 ]]; then
  args+=(--editable)
fi
args+=(odin)

echo "odin: installing from $ROOT${EDITABLE:+ (editable)}"
uv "${args[@]}"
echo
command -v odin
odin --version
