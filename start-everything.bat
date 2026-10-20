@echo off
rem ---------------------------------------------------------------------
rem  One click: opens the local control panel (http://127.0.0.1:8099)
rem  and starts backend (8080) + agent (8000) + web (5173) in the background
rem  - no console windows. The local model (18241) and its chat page (8090)
rem  start only when agent\.env points the agent at the local model.
rem
rem  Everything is started/stopped/switched from the panel page, including
rem  the LLM (remote API / local model / rules only) and the Amap keys.
rem  pythonw only runs the panel itself; services run under python.exe
rem  (see find_python in launcher\panel.py for why).
rem  Running this again while the panel is up just reopens the page.
rem  ASCII-only on purpose; see the note in start-model-ninfer.bat.
rem  Details (in Chinese): launcher\panel.py
rem ---------------------------------------------------------------------

cd /d "%~dp0"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" "%~dp0launcher\panel.py" --autostart
