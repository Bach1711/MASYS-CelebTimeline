@echo off
title MASYS CelebTimeline - Dong goi EXE (Cach 1)
cd /d "%~dp0"

set PYTHON_EXE=python
if exist "C:\Python314\python.exe" set PYTHON_EXE="C:\Python314\python.exe"

echo ======================================================================
echo   MASYS CelebTimeline - DONG GOI FILE EXE (CACH 1 - PORTABLE)
echo ======================================================================

%PYTHON_EXE% -m PyInstaller --noconfirm --clean --onedir --name "MASYS-CelebTimeline" --exclude-module app --exclude-module celeb_service --exclude-module face_engine --exclude-module youtube_pipeline --exclude-module renderer --collect-all yt_dlp --collect-all youtube_transcript_api --collect-all cv2 --collect-all fastapi --collect-all uvicorn --collect-all starlette --collect-all pydantic launcher.py

if errorlevel 1 (
    echo [LOI] Dong goi PyInstaller that bai!
    exit /b 1
)

echo [2/3] Dang sao chep ma nguon dong, giao dien, AI models va FFmpeg...
set OUT_DIR=MASYS-CelebTimeline_EXE
if exist "%OUT_DIR%" rmdir /s /q "%OUT_DIR%"
xcopy /E /I /Y /Q "dist\MASYS-CelebTimeline" "%OUT_DIR%" >nul

copy /Y "app.py" "%OUT_DIR%\" >nul
copy /Y "celeb_service.py" "%OUT_DIR%\" >nul
copy /Y "face_engine.py" "%OUT_DIR%\" >nul
copy /Y "youtube_pipeline.py" "%OUT_DIR%\" >nul
copy /Y "renderer.py" "%OUT_DIR%\" >nul
copy /Y "launcher.py" "%OUT_DIR%\" >nul
copy /Y "version.json" "%OUT_DIR%\" >nul
copy /Y "requirements.txt" "%OUT_DIR%\" >nul
copy /Y "tracker.html" "%OUT_DIR%\" >nul
if exist "feedback.json" copy /Y "feedback.json" "%OUT_DIR%\" >nul

xcopy /E /I /Y /Q "static" "%OUT_DIR%\static" >nul
xcopy /E /I /Y /Q "models" "%OUT_DIR%\models" >nul

mkdir "%OUT_DIR%\bin" >nul 2>nul
%PYTHON_EXE% -c "import shutil, youtube_pipeline, os; [shutil.copy2(os.path.realpath(p), os.path.join('MASYS-CelebTimeline_EXE', 'bin', os.path.basename(p).lower())) for p in [youtube_pipeline.FFMPEG_BIN, youtube_pipeline.FFPROBE_BIN] if p and os.path.exists(p)]"

echo [3/3] Don dep thu muc build tam...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "MASYS-CelebTimeline.spec" del /f /q "MASYS-CelebTimeline.spec"

echo.
echo ======================================================================
echo   HOAN TAT! File EXE nam tai:
echo   %CD%\%OUT_DIR%\MASYS-CelebTimeline.exe
echo ======================================================================
if not "%1"=="--nopause" pause
