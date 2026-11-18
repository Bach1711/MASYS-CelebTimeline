@echo off
chcp 65001 >nul
title MASYS CelebTimeline — Day ban cap nhat len GitHub
cd /d "%~dp0"
echo ========================================================
echo   MASYS CelebTimeline — PHAT HANH BAN CAP NHAT (DEV)
echo ========================================================
set /p COMMIT_MSG="Nhap ghi chu cap nhat (Bam Enter de dung mac dinh): "
if "%COMMIT_MSG%"=="" set COMMIT_MSG=Cap nhat MASYS CelebTimeline

git add -A
git commit -m "%COMMIT_MSG%"

if exist "C:\Python314\python.exe" (
    "C:\Python314\python.exe" -c "import app; app._get_local_version_info()"
) else (
    python -c "import app; app._get_local_version_info()" 2>nul
)

git add version.json
git commit --amend --no-edit >nul 2>nul
git push origin main

echo.
echo ========================================================
echo   HOAN TAT! Cac may khac chi can bam "Kiem tra Update"
echo   va bam "Cap nhat ngay" tren giao dien de cap nhat.
echo ========================================================
pause
