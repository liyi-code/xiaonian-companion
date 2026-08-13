@echo off
REM 启动「小念 ⇄ 3D 游戏」事件桥（多 NPC 版）
REM 先确保 venv 已建好、依赖已装（websockets / Pillow / 本地 Ollama 或云端 LLM key 已配置）
chcp 65001 >nul
setlocal
set PYTHONUTF8=1
cd /d %~dp0

if not exist venv\Scripts\python.exe (
    echo [桥] 未发现 venv，请先运行 启动.bat 自举环境
    pause
    exit /b 1
)

echo [桥] 启动多 NPC 事件桥（默认 ws://127.0.0.1:8765）...
venv\Scripts\python.exe -m src.bridge --port 8765
if errorlevel 1 (
    echo [桥] 启动失败，按任意键退出
    pause
)
endlocal
