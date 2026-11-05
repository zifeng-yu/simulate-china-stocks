#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"
if ! command -v uv >/dev/null 2>&1; then
    echo '请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/' >&2
    exit 1
fi
export UV_CACHE_DIR="$PROJECT_ROOT/.cache/uv"
uv sync --locked --group dev
"$PROJECT_ROOT/.venv/bin/python" -m china_stock_sim --root "$PROJECT_ROOT" init
"$PROJECT_ROOT/.venv/bin/python" -m china_stock_sim --root "$PROJECT_ROOT" doctor
