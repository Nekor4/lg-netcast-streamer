@echo off
chcp 65001 >nul
title Universal DLNA Full HD 1080p60 Screen + Audio Streamer
cd /d "%~dp0"

:: Automatyczne sprawdzenie i instalacja bibliotek przy pierwszym uruchomieniu na nowym PC
python -c "import mss, PIL, imageio_ffmpeg, pyaudiowpatch" 2>nul
if %errorlevel% neq 0 (
    echo [Instalacja wymaganych bibliotek Python dla tego komputera...]
    pip install -r requirements.txt
)

python netcast_hw_streamer.py --port 8000 --monitor 1 --fps 60 --height 1080 --bitrate 7M --encoder auto
pause
