@echo off
rem 启动 PotSync 中继服务器（默认监听 0.0.0.0:8765）
cd /d "%~dp0"
python server\server.py --host 0.0.0.0 --port 8765
pause
