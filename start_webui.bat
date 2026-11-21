@echo off
setlocal
REM CV Tailor web UI - double-click to start, then open http://127.0.0.1:8765
cd /d "%~dp0"
if not exist webui\.venv\Scripts\python.exe (
  echo First run: creating webui\.venv and installing packages...
  python -m venv webui\.venv || goto :nopython
)
REM Install anything new in requirements.txt (quick when nothing changed).
webui\.venv\Scripts\python -m pip install -q --disable-pip-version-check -r webui\requirements.txt || (echo Package install failed: check your internet connection. & pause & goto :eof)
start "" http://127.0.0.1:8765
webui\.venv\Scripts\python webui\app.py
pause
goto :eof
:nopython
echo Python 3.10+ is required. Install it from python.org and tick "Add to PATH".
pause
