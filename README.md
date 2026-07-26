# 小念 · 本地版（AI 女友桌面程序）

一个**完全可本地离线运行**的 AI 女友桌面程序（Windows）。基于本地大模型 [Ollama](https://ollama.com)，
支持 Live2D 形象、语音、屏幕陪伴、长期记忆与自主行为。你的聊天记录、记忆、模型全部留在自己电脑上，
不上传任何云端（除非你主动配置云端模型）。

> 这是「本地版」：默认用本机 Ollama 跑模型，开箱即用、隐私可控。仓库已自带可商用的 Live2D 模型
> **桃濑日和 / hiyori_pro**。语音默认关闭，可自选开启。

---

## ✨ 功能特性

- 💬 自然聊天，温柔体贴的女友人设
- 🧠 **意识层（本地推理加速）**：语义联想、长期记忆、性格情感，越聊越懂你
- 💾 **检索增强记忆（RAG）+ 长对话压缩**：聊再久也不丢上下文、不前后矛盾
- 🛠 **主动关心 + 受约束自主权限**：只在白名单内自调参（如更频繁提醒休息），绝不碰系统 / 代码 / 你的文件
- 🎭 **Live2D 形象**：官方免费可商用模型 hiyori_pro，实时口型、表情、动作
- 🔊 **可选语音**：本地 GPT-SoVITS 克隆音色（默认关，可不开）
- 🖥 **屏幕陪伴 + 多模态视觉**：感知你在用什么软件、甚至“看懂”屏幕画面
- 💬 **可接入 QQ / 微信**：让她用自己账号陪你聊
- 📂 **帮你操作电脑**：开软件、搜文件、建计划 / 笔记文件

---

## 🧰 环境要求

- Windows 10 / 11
- Python **3.10+**（安装时务必勾选 “Add python.exe to PATH”）
- [Ollama](https://ollama.com)（本地大模型运行环境）
- 可选：NVIDIA 显卡（语音 / GPU 加速更流畅）、麦克风（语音输入）

---

## 🚀 快速开始（本地 Ollama）

1. **安装并启动 Ollama**，拉取一个对话模型（推荐，约 9GB）：
   ```bash
   ollama pull qwen2.5:14b
   ```
   > 备选：`deepseek-r1:32b`（更强但更慢更吃显存）、`qwen2.5:7b`（轻量）。
   > 想让模型常驻内存、避免反复冷加载，可设系统环境变量 `OLLAMA_KEEP_ALIVE=-1`（重启 Ollama 生效）。

2. **安装 Python 3.10+**（含 pip，已加入 PATH）。

3. **获取本仓库**：
   ```bash
   git clone https://github.com/liyi-code/ai-girlfriend-local.git
   cd ai-girlfriend-local
   ```

4. **双击 `启动.bat`**：
   - 首次运行自动创建 `venv` 虚拟环境并安装依赖；
   - 若没有 `.env`，会自动从 `.env.example` 复制一份（已默认指向本地 Ollama）。

5. **（可选）确认 `.env` 里的对话模型指向本地 Ollama**（默认已是，无需改）：
   ```ini
   OPENAI_API_KEY=ollama
   OPENAI_BASE_URL=http://localhost:11434/v1
   MODEL=qwen2.5:14b
   ```
   > 也可以启动后点输入条上的 `◐` → 「API 设置」里填，还能“测试连接”。

6. **再次双击 `启动.bat`** 即可开始聊天。

> ⏳ 首次发消息前，小念会冷加载本地模型，约需十几秒到一两分钟（取决于模型大小与显卡）。
> 加载期间输入条会提示“模型加载中”。加载完成后常驻内存，之后秒回。

---

## ⚙️ 配置详解

所有配置在 `.env`（首次运行由 `.env.example` 复制生成）。常用项：

| 配置 | 说明 |
|------|------|
| `OPENAI_BASE_URL` / `MODEL` / `OPENAI_API_KEY` | 对话模型。默认本地 Ollama；也可改成 DeepSeek / OpenAI / LM Studio 等任意 OpenAI 兼容服务 |
| `LIVE2D_ENABLED` / `LIVE2D_MODEL` | Live2D 形象，默认 `assets/live2d/hiyori_pro/runtime/hiyori_pro_t11.model3.json`（已自带，可商用） |
| `VOICE_INPUT_ENABLED` / `ASR_BACKEND` | 语音输入，本地 faster-whisper 离线识别 |
| `VOICE_OUTPUT_ENABLED` / `SOVITS_HOME` | 语音输出；`SOVITS_HOME` 留空即关闭 |
| `SCREEN_WATCH_ENABLED` / `VISION_ENABLED` | 屏幕陪伴 / 多模态视觉 |
| `AUTONOMY_ENABLED` / `FILE_OPS_ENABLED` | 受约束自主权限 / 帮你建文件 |

完整选项与跨电脑部署、语音、QQ / 微信接入说明见 **`SETUP.md`**。

---

## 📁 目录结构

```
ai-girlfriend-local/
├─ src/                # 程序源码（assistant / gui / live2d / clayer 意识层 / tools …）
├─ assets/live2d/     # Live2D 模型（hiyori_pro，已含，可商用）
├─ onebot/            # QQ 接入示例
├─ docs/              # 额外文档
├─ .env.example       # 配置模板（已默认本地 Ollama）
├─ requirements.txt   # Python 依赖
├─ 启动.bat           # 一键启动（建 venv / 装依赖 / 生成 .env）
├─ SETUP.md           # 详细部署指南
├─ README.md
└─ LICENSE
```

> 运行时生成的 `data/`（聊天记录、记忆、状态）、`venv/`、`models/`、`personal_backup_*/` 等
> 已被 `.gitignore` 忽略，不会进入仓库。

---

## ❓ 常见问题

- **首次回复很慢 / 没反应？** 本地模型正在冷加载，等加载完成（输入条有提示）即可；
  设 `OLLAMA_KEEP_ALIVE=-1` 可让模型常驻，之后秒回。
- **想换模型？** 改 `.env` 的 `MODEL` 或点输入条 `◐` 切换。
- **没有 GPU？** 仍可用 CPU 跑（较慢）；语音 `SOVITS_DEVICE` 留空会自动探测。
- **不要提交 `.env`！** 里面可能含你的密钥，已被 `.gitignore` 忽略；分享给别人请用 `.env.example`。

---

## ⚠️ 版权与声明

- **Live2D 形象**：使用 Cubism 官方免费示例模型 **桃濑日和 / hiyori_pro**，可个人 / 参赛非商用使用。
- **语音**：默认关闭；若开启 GPT-SoVITS 克隆音色，请使用你**自己授权**的参考音频，相关版权责任由使用者自行承担。
- 本项目仅供学习 / 个人陪伴用途，请勿用于商业或骚扰等场景。

---

## 📄 许可证

本项目以 [MIT 许可证](LICENSE) 开源。
