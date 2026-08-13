@echo off
chcp 65001 >nul
title 小念小镇 - 完整启动
echo ============================================
echo   我的世界村庄式自给自足小镇
echo   Python 大脑(bridge+town) + Unity 2022 场景
echo ============================================
echo.
echo [1/3] 启动 Python 大脑（bridge + 小镇经济模拟）
start "小念大脑" cmd /k "venv\Scripts\python.exe -m src.bridge --port 8765"
echo       已后台启动，等待初始化（约 10-20 秒）...
timeout /t 15 >nul
echo.
echo [2/3] 请在 Unity 2022 编辑器里：
echo   1) 打开 unity_project 工程（D:\AI训练\ai-girlfriend-本地版-备份-20260724\unity_project）
echo   2) Package Manager 装 Unity-Skills（Git URL 见 README）
echo   3) Window > UnitySkills > 点开关启动 REST Server（localhost:8090）
echo.
set /p READY="Unity Server 已启动？(y 继续 / n 跳过场景搭建): "
if /i "%READY%"=="y" (
  echo [3/3] 用 Unity-Skills 一键搭建村庄场景...
  venv\Scripts\python.exe unity_client/unity_skills/build_village.py
) else (
  echo 跳过场景搭建，你可以稍后手动运行 build_village.py
)
echo.
echo 完成！Unity 里应能看到村庄场景；控制台可看 town_state 实时广播。
pause
