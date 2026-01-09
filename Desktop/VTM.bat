@echo off
yt-dlp --rm-cache-dir
setlocal
cd /d "%~dp0"
echo Levantando VTM...
python vtm.py
echo.
echo ==========================================
echo El proceso de Python ha terminado.
pause
