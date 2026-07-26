# 小念（ai-girlfriend）跨电脑部署指南

本项目的配置全部支持**相对路径**，拷到别的电脑（甚至不同盘符）后，只要不写死绝对路径就能直接跑。

## 一、最简运行（纯文本对话，无需语音/形象）

1. 在目标电脑安装 **Python 3.10+**，安装时勾选「Add python.exe to PATH」。
2. 把整个 `ai-girlfriend` 文件夹复制过去。
3. 双击 **`启动.bat`**：
   - 首次会自动创建 `venv` 虚拟环境、安装 `requirements.txt` 依赖；
   - 若没有 `.env`，会自动从 `.env.example` 复制一份并提示你填写。
4. 打开 `.env`，至少填好 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL`
   （也可以启动后点输入条上的 `◐` → 「API 设置」里填，还能“测试连接”）。
5. 再次双击 `启动.bat` 即可聊天。

> 没有填 Key 也能启动：点 ◐ 打开「API 设置」填好保存即可，不会一启动就崩。

## 二、开启 Live2D 形象

- `.env` 里 `LIVE2D_ENABLED=true`，`LIVE2D_MODEL` 用相对路径（如 `assets/live2d/hiyori_pro/runtime/hiyori_pro_t11.model3.json`，已随项目附带，官方免费可商用）。
- 形象模型文件已在 `assets/live2d/` 内，直接可用。

## 三、开启语音（可选，依赖较重）

### 3.1 语音输出（GPT-SoVITS 克隆声音）
需要你**自备 GPT-SoVITS 仓库**（项目不含其权重，体积很大）：
- 推荐把仓库放在 `ai-girlfriend` 的**同级目录**（如 `D:\xxx\GPT-SoVITS-...`），
  这样 `.env` 里 `SOVITS_HOME` 用相对路径 `../GPT-SoVITS-.../GPT-SoVITS-...` 即可，
  整包拷贝到别的电脑也不用改路径；
- `SOVITS_REF_AUDIO` / `SOVITS_REF_TEXT` 填你的参考音频与文本（参考音频放项目根目录，用相对路径如 `ref_yae_clean.wav`）；
- `SOVITS_DEVICE` **留空自动探测**（有 N 卡用 cuda，否则 cpu），换电脑不用改；
- 未配置 `SOVITS_HOME` 时语音输出自动禁用，其余功能照常。

### 3.2 语音输入（本地识别，离线免费）
- `.env` 里 `ASR_BACKEND=local`；
- 识别模型已接入**一键自举**：双击 `启动.bat` 时，若检测到你开启了语音输入且模型未下载，
  会自动从国内镜像拉取 faster-whisper 模型（断点续传；可选文件拉不到会自动跳过，不影响识别）。
  也可手动跑：
  ```
  venv\Scripts\python.exe download_model.py
  ```
- 麦克风设备留空会自动选择；若识别不到，在设置面板(◐)的麦克风下拉里手动选。

## 四、其它可选能力
- **屏幕陪伴**：`SCREEN_WATCH_ENABLED=true`（默认开），无需额外依赖。
- **多模态视觉（看懂屏幕）**：`VISION_ENABLED=true` 并填 `VISION_API_KEY`（独立于对话 Key，推荐智谱 GLM-4V-Flash 免费档）。

## 五、换电脑注意事项
- 所有路径优先写**相对路径**（相对项目根），避免 `D:\...` 这种写死绝对路径。本项目的 `.env` 已默认用相对路径。
- 语音输出依赖 GPT-SoVITS 仓库：把它放在 `ai-girlfriend` 的**同级目录**，配合 `.env` 里的 `../GPT-SoVITS-...` 相对路径，整包拷贝后无需改任何路径。
- `SOVITS_DEVICE` 留空即可自动适配有无显卡。
- `.env` 含你的 API 密钥，**不要外传 / 不要提交到公开仓库**；分享给别人用 `.env.example` 模板。
- 若从旧机器拷贝，记得把你的参考音频（如 `ref_yae_clean.wav`）一并放进项目根目录。
