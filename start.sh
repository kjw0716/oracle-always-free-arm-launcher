#!/bin/sh
set -eu
cd "$(dirname "$0")"
export PYTHONUTF8=1
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
if ! .venv/bin/python -c "import oci; assert oci.__version__ == '2.189.0'" >/dev/null 2>&1; then
    .venv/bin/python -m pip install --no-compile -r requirements.txt
fi
if [ "$#" -eq 0 ]; then
    exec .venv/bin/python grab.py --menu
fi
exec .venv/bin/python grab.py "$@"
