@echo off
rem ============================================================
rem  PotSync 打包脚本（PyInstaller，纯 64 位，单文件 exe）
rem
rem  产物 dist\PotSync.exe = 客户端 + 内置中继服务器一体，
rem  双击即用，无需安装 Python。
rem
rem  体积/速度优化：onefile + UPX 压缩（若 tools\upx\upx.exe 存在）
rem  + 剔除未用到的 Qt 模块。
rem
rem  环境：64 位 Python 3.9+，先执行一遍：
rem      python -m pip install -r requirements.txt pyinstaller
rem ============================================================
cd /d "%~dp0"

python -m pip install -r requirements.txt pyinstaller || goto :err

set UPX_ARG=
if exist tools\upx\upx.exe set UPX_ARG=--upx-dir tools\upx

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name PotSync ^
    --add-data "client\resources;resources" ^
    %UPX_ARG% ^
    --exclude-module tkinter ^
    --exclude-module PyQt5.QtNetwork ^
    --exclude-module PyQt5.QtQml ^
    --exclude-module PyQt5.QtQuick ^
    --exclude-module PyQt5.QtSql ^
    --exclude-module PyQt5.QtTest ^
    --exclude-module PyQt5.QtMultimedia ^
    --exclude-module PyQt5.QtSvg ^
    --exclude-module PyQt5.QtXml ^
    --exclude-module PyQt5.QtWebEngineWidgets ^
    --exclude-module PyQt5.QtWebSockets ^
    client\app.py || goto :err

echo.
echo 打包完成： dist\PotSync.exe
goto :eof

:err
echo.
echo 打包失败，请检查上方错误信息。
pause
