@echo off
REM 在 Windows 上把 Agent 打包成免安装单文件 exe（需已装 Python 3）。
REM 产出: dist\PhonesInventory-DeviceAgent.exe，拷给各门店双击即用。
cd /d "%~dp0"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
pyinstaller --onefile --console --name PhonesInventory-DeviceAgent agent.py
echo.
echo 完成！exe 在 dist\PhonesInventory-DeviceAgent.exe
pause
