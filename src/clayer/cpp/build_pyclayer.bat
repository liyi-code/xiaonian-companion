@echo off
REM 小念意识层 C++ 加速库构建（Windows 薄包装）。真正逻辑在 build_pyclayer.py。
cd /d "%~dp0"
py -3 build_pyclayer.py 2>nul || python build_pyclayer.py 2>nul || "%~dp0..\..\..\venv\Scripts\python.exe" build_pyclayer.py
if errorlevel 1 pause
