@echo off
REM 双击运行（Windows）。需先装好 Python 3 和 Apple Mobile Device Support(随 iTunes)。
cd /d "%~dp0"
python -m pip install -r requirements.txt
python agent.py
pause
