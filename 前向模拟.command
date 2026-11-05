#!/bin/sh
set -eu
PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON="$PROJECT_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo '请先在新仓库目录运行 ./scripts/setup.sh 建立独立环境。' >&2
    exit 1
fi
"$PYTHON" -m china_stock_sim --root "$PROJECT_ROOT" dashboard-refresh --open
