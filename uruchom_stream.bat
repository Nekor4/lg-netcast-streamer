@echo off
chcp 65001 >nul
title Universal DLNA Full HD 1080p60 Screen + Audio Streamer (GUI)
cd /d "%~dp0"

:: Automatyczne sprawdzenie i instalacja bibliotek przy pierwszym uruchomieniu na nowym PC
python -c "import mss, PIL, imageio_ffmpeg, pyaudiowpatch" 2>nul
if %errorlevel% neq 0 (
    echo [Instalacja wymaganych bibliotek Python dla tego komputera...]
    pip install -r requirements.txt
)

start "" pythonw netcast_gui.py
