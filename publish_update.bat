@echo off
chcp 65001 >nul
title MASYS CelebTimeline — Day ban cap nhat len GitHub
cd /d "%~dp0"
echo ========================================================
echo   MASYS CelebTimeline — PHAT HANH BAN CAP NHAT (DEV)
echo ========================================================
set PYTHON_EXE=python
if exist "C:\Python314\python.exe" set PYTHON_EXE="C:\Python314\python.exe"

for /f "delims=" %%V in ('%PYTHON_EXE% -c "import app; print(app._bump_ver_label(app._get_local_version_info().get('version')))"') do set NEXT_VER=%%V
if "%NEXT_VER%"=="" set NEXT_VER=ver 0.2

set /p INPUT_VER="Nhap so phien ban (Enter de dung mac dinh '%NEXT_VER%', VD: ver 0.2, ver 3.5): "
if "%INPUT_VER%"=="" set INPUT_VER=%NEXT_VER%

set /p COMMIT_MSG="Nhap ghi chu cap nhat (Bam Enter de dung mac dinh): "
if "%COMMIT_MSG%"=="" set COMMIT_MSG=Release %INPUT_VER%

%PYTHON_EXE% -c "import json; d=json.load(open('version.json',encoding='utf-8')); import app; d['version']=app._normalize_ver_label(r'''%INPUT_VER%'''); json.dump(d,open('version.json','w',encoding='utf-8'),ensure_ascii=False,indent=2)"

git add -A
git commit -m "%COMMIT_MSG%"
%PYTHON_EXE% -c "import app; app._get_local_version_info()"
git add version.json
git commit --amend --no-edit >nul 2>nul
git push origin main

echo.
echo ========================================================
echo   HOAN TAT (%INPUT_VER%)! Cac may khac chi can bam
echo   "Kiem tra Update" -> "Cap nhat ngay" tren giao dien.
echo ========================================================
pause
