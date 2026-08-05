# 小念 AI 女友 · 本地版 项目总览（2026-08-05 快照）

## 项目概览
- **路径**：`d:\AI训练\ai-girlfriend-本地版-备份-20260724`（GitHub 仓库 `liyi-code/ai-girlfriend-local`，main 分支）
- **形态**：Windows 桌面 AI 女友「小念」，可**完全本地离线**运行
- **技术栈**：
  - 对话大脑：本地大模型 [Ollama](https://ollama.com)（默认 `qwen2.5:14b`），OpenAI 兼容接口
  - 前端形象：**Unity**（C# 驱动 VRM/Live2D 角色，含程序化动画 `ConceptStateMachine.cs`）+ 可选 Live2D 桌宠
  - 认知框架：**意识层 `src/clayer/`**（联想/注意力/遗忘/价值导向，不改模型权重）
  - 情绪情感：**`src/emotion.py`**（5 维情绪 + 5 种性格，驱动对话语气与肢体动作）
  - 语音：本地 faster-whisper(ASR) + 可选 GPT-SoVITS(TTS)
  - 屏幕陪伴：程序级（窗口标题/进程名）+ 可选像素级视觉（GLM-4V 等）
  - 通信：Python `websockets` 桥（`src/bridge.py`）↔ Unity WebSocket 客户端（`NpcBridgeClient.cs`）
- **入口**：`venv\Scripts\python.exe -m src.main` 或双击 `启动.bat`

## 源码结构（src/）
| 文件 | 职责 |
|------|------|
| `main.py` | 启动入口，拉起桥 + 各 NPC |
| `assistant.py` | 大脑：意图路由 + LLM + 工具循环 + 意识层接入（think/learn/sleep）+ 性格情感注入 |
| `bridge.py` | WebSocket 桥：NPC 管理、动作下发、视觉快照联合、自主探索驱动、情绪→动作权重融合 |
| `emotion.py` | 情绪引擎（5 维）+ 性格系统（5 种）+ `TRAIT_MOTION` 动作偏好 + `motion_params()` |
| `memory.py` | 长期记忆：归档 + TF-IDF 倒排索引（RAG）+ 长期记忆压缩 |
| `villagers.py` | NPC 引擎：预加载、立绘/语音预热、人口管理 |
| `town.py` / `quest.py` | 小镇世界状态 + 任务/目标系统（MMO+RPG 要素） |
| `explorer.py` | 自主探索：周期探索世界、生成自主行为、自发对话 |
| `world_state.py` | 结构化世界状态（在玩什么/用了多久等 symbolic 事实） |
| `screen_watch.py` | 屏幕陪伴：窗口/进程感知 |
| `vision.py` | 多模态视觉：截图 + 视觉模型理解画面（可选） |
| `autonomy.py` | 受约束自主权限：白名单自调参 + 健康护栏 |
| `tools.py` | 工具注册（开软件/搜文件/建文件/记忆/发消息…） |
| `voice.py` | 语音输入/输出 |
| `clayer/` | 意识层（详见 `docs/架构与数据流.md`） |

## 已完成的重大能力
1. **NPC 预加载**：`villagers.py` `_preloaded` + `preload_all()`，首连接/首消息前预热立绘与语音；NPC 资源懒加载 + 首预热，不阻塞首聊。
2. **自主行动（多套）**：
   - `explorer.py` 自主探索：周期生成自主行为、自发对话、记忆检索后再行动；
   - `bridge.py` 待机久了的 `restlessness` 自晃动 + `idle` 心跳 + `screen_watch` 正反馈（看你用多久主动搭话）；
   - `autonomy.py` 受约束自主权限：作息/设备大调整弹窗确认，仅白名单内自调参，健康护栏不迎合有害行为。
3. **数据 + 截屏联合理**：`bridge.py _handle_visual_snapshot` 把**程序级 symbolic 数据**（窗口标题/进程名/连续时长，`world_state`）与**像素级截屏**（`vision.describe_screen`）联合，先拿结构化事实再叠加视觉模型看懂的画面（输赢/升级/报错文字），一起喂 LLM。
4. **动作受性格情感权重影响（2026-08-05 完善）**：
   - 情感（情绪 `joy` 兴奋度）驱动动作 speed/amplitude（开心→更快更大）；
   - **性格（`TRAIT_MOTION`）驱动动作风格**：活泼→轻快大幅+爱挥手、黏人→身体前倾+倾向靠近、傲娇→略后缩+爱别过脸、敏感→最慢最小偏低头、温柔→舒缓克制；
   - 端到端闭环：Python `emotion.motion_params()` → `bridge.compute_action_params()` 融合 `speed/amplitude/lean/trait/micro` → Unity `NpcBridgeClient` 转发 → `ConceptStateMachine.TriggerAction(trait, lean)` 把倾向折算进骨骼（上臂前举/上身前倾）。
5. **意识层 clayer 已接入运行**：`assistant.py` 在对话前 `mind.think()`（多念竞争 + 价值导向引导）、对话后 `learn_async()` 回写、睡眠触发 `consolidate_memory()`+`save()`；受 `.env` 的 `CONSCIOUSNESS_ENABLED` 开关控制（需为 `true` 且模型为 qwen 系列才生效，Ollama 后端真正用 logit_bias）。
6. **检索增强记忆（RAG）+ 长对话压缩**：`memory.py` 归档 + 倒排索引，`_maybe_compress` 后台把旧对话总结成长期记忆要点。
7. **实时口型 + 程序化动画（Unity）**：`ConceptStateMachine.cs` 实现呼吸/挺直修正/挥手（钟摆+跟随)/点头/环顾/转身/立正，带 Slerp 阻尼与动作尾段平滑收尾，杜绝瞬抬瞬落。

## 待续 / 已知缺口
- **Unity 动画“没反应”排查**：若改代码后表现不变，先确认 Unity Console 无红色编译错、重新点 ▶ 进入 Play、`Auto Test On Start` 勾选可自检挥手；脚本需真正挂到角色物体上。
- 微信代发 transport 为占位（源码曾丢失），QQ 可用。
- 视觉(VISION_ENABLED)默认关，需自备视觉 API Key 才生效。
- `pyclayer.pyd`（C++ 加速）不入库，换机器需重新编译；纯 Python 回退保证可跑。

## 与“主项目 / 便携版”的区别
- 本目录是**本地版备份**：全本地 Ollama、无云端依赖、含 Unity 形象工程 `unity_project/`。
- 主项目 `d:\AI训练\ai-girlfriend` 与便携参赛版 `d:\AI训练\ai桌面女友\ai-girlfriend` 为云端 API 版（DeepSeek/Seed-TTS），代码同源但模型/语音后端不同。
