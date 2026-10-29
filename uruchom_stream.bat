@echo off
chcp 65001 >nul
title LG 55LM670S-ZA Hardware Full HD 1080p60 (RTX 3070 Ti ddagrab) + Audio
cd /d "%~dp0"
python netcast_hw_streamer.py --port 8000 --monitor 1 --fps 60 --height 1080 --bitrate 7M
pause
