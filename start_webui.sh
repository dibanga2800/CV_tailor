#!/usr/bin/env bash
# CV Tailor web UI - then open http://127.0.0.1:8765
set -e
cd "$(dirname "$0")"
if [ ! -d webui/.venv ]; then
  python3 -m venv webui/.venv
fi
webui/.venv/bin/pip install -q --disable-pip-version-check -r webui/requirements.txt
exec webui/.venv/bin/python webui/app.py
