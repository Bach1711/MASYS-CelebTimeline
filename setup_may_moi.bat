@echo off
chcp 65001 >nul
title MASYS CelebTimeline — Cai dat moi truong cho May Moi (First-Time Setup)
cd /d "%~dp0"
echo ======================================================================
echo   MASYS CelebTimeline — CAI DAT MOI TRUONG LAN DAU CHO MAY TINH MOI
echo ======================================================================
echo.

REM 1. Tim Python tren may
set PYTHON_EXE=
if exist "C:\Python314\python.exe" (
    set PYTHON_EXE="C:\Python314\python.exe"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set PYTHON_EXE=python
    ) else (
        where py >nul 2>nul
        if %errorlevel%==0 (
            set PYTHON_EXE=py -3
        )
    )
)

if "%PYTHON_EXE%"=="" (
    echo [!] Chua tim thay Python tren may tinh nay.
    echo [*] Dang tu dong cai dat Python 3.12 qua Windows Winget...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    set PYTHON_EXE=python
)

echo [1/3] Dang cai dat cac thu vien Python trong requirements.txt...
%PYTHON_EXE% -m pip install --upgrade pip --quiet
%PYTHON_EXE% -m pip install -r requirements.txt
echo.

echo [2/3] Dang kiem tra cong cu xu ly video FFmpeg...
where ffmpeg >nul 2>nul
if %errorlevel% neq 0 (
    if not exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" (
        echo [*] Chua co FFmpeg. Dang tu dong cai dat FFmpeg qua Windows Winget...
        winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    ) else (
        echo [OK] Da tim thay FFmpeg tai WinGet Links.
    )
) else (
    echo [OK] Da tim thay FFmpeg trong he thong.
)
echo.

echo [3/3] Kiem tra mo hinh nhan dien khuon mat AI (OpenCV YuNet + SFace)...
if exist "models\face_detection_yunet_2023mar.onnx" (
    if exist "models\face_recognition_sface_2021dec.onnx" (
        echo [OK] Da co day du 2 mo hinh AI trong thu muc models/.
    )
)
echo.
echo ======================================================================
echo   HOAN TAT CAI DAT!
echo   Tu bay gio ban chi can nhap dup vao "start_studio.bat" de su dung!
echo ======================================================================
pause
