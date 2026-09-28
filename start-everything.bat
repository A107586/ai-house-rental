@echo off
rem ---------------------------------------------------------------------
rem  One click: opens the local control panel (http://127.0.0.1:8099)
rem  and starts model (18241) + backend (8080) + agent (8000) + web (5173)
rem  + model chat page (8090) in the background - no console windows.
rem
rem  Everything is started/stopped/switched from the panel page.
rem  Running this again while the panel is up just reopens the page.
rem  ASCII-only on purpose; see the note in start-model-ninfer.bat.
rem  Details (in Chinese): launcher\panel.py
rem ---------------------------------------------------------------------

cd /d "%~dp0"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" "%~dp0launcher\panel.py" --autostart
