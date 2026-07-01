@echo off
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

echo ============================================================
echo   Keyword Cleaner - Variant Review GUI
echo ============================================================
echo.

python gui_review.py %*
pause
