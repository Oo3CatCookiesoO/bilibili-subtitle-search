@echo off
cd /d "%~dp0"
echo http://127.0.0.1:8765|clip
echo Web UI link copied to clipboard: http://127.0.0.1:8765
python web_app.py
pause
