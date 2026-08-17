# 小念 × Unity(VRM) 接入指南

把「小念的大脑」（LLM + 情绪 + 意识层 + 记忆 + 语音）接入一个 3D 游戏，让她有
**独立建模、按输入做动作/语言/表情**，并具备 **自主世界感知 + 主动探索** 能力。

## 1. 架构

```
┌─ Unity 客户端（unity_client/*.cs）─────────────┐
│  XiaonianBridge  事件桥（WebSocket ↔ 小念大脑）  │
│  SymbolicPerception  符号感知采集 + 低频视觉快照 │  ← 遍历场景，推结构化文本（无图像）
│  AgentController   执行主动探索指令（移动/看/交互）│
│  PerceptTag        标注“可被小念感知”的物体       │
│  VRM 模型 + Animator + AudioSource + 相机         │
└───────────────────┬────────────────────────────┘
                     │ WebSocket JSON (ws://127.0.0.1:8765)
┌────────────────────┴───────────────────────────┐
│ 后端（Python，src/）                              │
│  bridge.py      事件桥（引擎无关）                │
│  action_library.py / osc_bridge.py  动作教学间     │
│  assistant/emotion/clayer/memory/voice  （大脑，不动）│
└─────────────────────────────────────────────────┘
```

## 2. 运行

```bat
:: 后端（小念大脑）
cd d:\AI训练\ai-girlfriend-本地版-备份-20260724
venv\Scripts\python.exe -m src.bridge          :: ws://127.0.0.1:8765
```

Unity 端：
1. 装 **UniVRM** 导入你的 `.vrm` 模型；装 **WebSocketSharp**（Assets 里放 WebSocketSharp.cs）。
2. 把 `XiaonianBridge.cs` + `AgentController.cs` 挂到 VRM 模型 GameObject（需同物体有
   `VRMBlendShapeProxy` + `Animator` + `AudioSource`）。
3. 把 `SymbolicPerception.cs` 挂到一个管理器物体（会自动找到 XiaonianBridge），
   并给场景中想让小念感知的物体挂 `PerceptTag.cs`。
4. （视觉快照）给 `SymbolicPerception` 挂一个相机当小念视角；不挂则只跑符号感知。
5. 运行，Inspector 填 `wsUrl=ws://127.0.0.1:8765`。打字即可对话。

## 3. 事件协议

### Unity → Python（感知 / 输入）
| 事件 | 字段 | 说明 |
|---|---|---|
| `user_input` | `text` | 玩家输入 |

### Python → Unity（输出 / 指令）
| 事件 | 字段 | 说明 |
|---|---|---|
| `token` | `text` | 字幕增量（流式） |
| `speech_start` | — | 开始说话（启用口型） |
| `audio` | `wav`(base64) | 播放并实时口型 |
| `emotion` | `dominant`(joy/anger/sadness/calm/anxiety) | 表情 Blendshape |
| `action` | `name`(jump/turn/wave/pat/nod) | Animator 触发器 |
| `talk_stop` | — | 口型归零、淡出气泡 |

## 4. 说明

> 旧版的「世界感知 + 主动探索」（world_state.py / explorer.py）已随 NPC 小镇一起废弃移除，
> 相关事件（world_load / symbolic_percept / visual_snapshot / agent_command）不再由 Python 端处理。

## 5. 相关配置（.env）
| 键 | 默认 | 说明 |
|---|---|---|
| `VISION_*` | — | 视觉 API 配置（同屏幕视觉） |

## 6. 你（项目方）要补的
- **预加载接线**：把你的场景流式加载/卸载接到 `SymbolicPerception.ReportRegion(regionId, loaded)`。
- **物体标注**：给可感知物体挂 `PerceptTag`（填 `id/name/type`）。
- **动画状态机**：建 `isMoving` 布尔、`interact` 触发器、`jump/turn/wave/pat/nod` 触发器。
- **VRM0 vs VRM1**：本客户端用 VRM0 的 `BlendShapePreset`；若用 VRM1(UniVRM 的 VRM10)，
  表情接口改为 `Vrm10Instance.LookAt` / `Vrm10Instance.Expression`，映射逻辑同构。
