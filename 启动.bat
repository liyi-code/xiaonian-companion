@echo off
chcp 936 >nul
cd /d "%~dp0"

:: 1) 没有虚拟环境就创建（需要本机已安装 Python 3.10+ 并加入 PATH）
if not exist "venv\Scripts\python.exe" (
    echo 首次运行：正在创建虚拟环境 venv ...
    python -m venv venv
    if errorlevel 1 (
        echo 创建 venv 失败：请先安装 Python 3.10+，安装时勾选“Add python.exe to PATH”。
        pause
        exit /b 1
    )
)

:: 2) 安装/补齐依赖（已满足时会很快跳过；离线且已装则可忽略报错）
echo 正在检查并安装依赖 ...
venv\Scripts\python.exe -m pip install --upgrade pip >nul 2>&1
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [警告] 依赖安装失败（可能是没联网）。若依赖已就绪可忽略；否则请联网后重跑本脚本。
)

:: 3) 没有 .env 就从模板生成一份，提醒用户填写
if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env >nul
        echo 已根据 .env.example 生成 .env，请打开填写你的 OPENAI_API_KEY 等配置，然后重新运行本脚本。
    ) else (
        echo 未找到 .env 与 .env.example，请手动创建 .env 配置文件。
    )
    pause
    exit /b 0
)

::: 3.5) 语音输入（本地 ASR）模型：若已开启且模型缺失，自动从国内镜像拉取（断点续传）。
:::       失败也不阻塞启动，只是离线语音输入暂不可用。
if exist ".env" (
    findstr /i /c:"VOICE_INPUT_ENABLED=false" .env >nul
    if not errorlevel 1 goto :skip_asr
    if not exist "models\faster-whisper-small\model.bin" (
        echo 正在自动下载语音识别模型（首次需联网，可断点续传，缺失不影响文字聊天）...
        venv\Scripts\python.exe download_model.py
    )
)
:skip_asr

echo 正在启动小念（AI 女友）...
venv\Scripts\python.exe -m src.main
pause
