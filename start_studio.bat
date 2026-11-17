@echo off
chcp 65001 >nul
title MASYS CelebTimeline (Port 8765)
cd /d "%~dp0"
echo ========================================================
echo   Dang khoi dong MASYS CelebTimeline...
echo   Trinh duyet se tu dong mo tai: http://127.0.0.1:8765
echo ========================================================
start "" "http://127.0.0.1:8765"
C:\Python314\python.exe app.py
pause
