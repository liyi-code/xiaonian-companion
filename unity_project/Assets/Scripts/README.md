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
│  world_state.py 符号感知工作记忆（只保留已加载范围）│
│  explorer.py    主动探索引擎（意识层驱动）         │
│  assistant/emotion/clayer/memory/voice  （大脑，不动）│
└─────────────────────────────────────────────────┘
```

## 2. 运行

```bat
:: 后端（小念大脑 + 世界感知 + 探索引擎）
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
5. 运行，Inspector 填 `wsUrl=ws://127.0.0.1:8765`。打字即可对话；不交互时小念会自己探索。

## 3. 事件协议

### Unity → Python（感知 / 输入）
| 事件 | 字段 | 说明 |
|---|---|---|
| `user_input` | `text` | 玩家输入 |
| `world_load` | `region_id, loaded` | 区域(预加载)加载/卸载 |
| `symbolic_percept` | `agent_pos, objects:[{id,name,type,pos,state,region}]` | 符号感知（**无图像**） |
| `visual_snapshot` | `cam_pos, image_b64` | 低频 1080p 视觉快照(base64) |

### Python → Unity（输出 / 指令）
| 事件 | 字段 | 说明 |
|---|---|---|
| `token` | `text` | 字幕增量（流式） |
| `speech_start` | — | 开始说话（启用口型） |
| `audio` | `wav`(base64) | 播放并实时口型 |
| `emotion` | `dominant`(joy/anger/sadness/calm/anxiety) | 表情 Blendshape |
| `action` | `name`(jump/turn/wave/pat/nod) | Animator 触发器 |
| `talk_stop` | — | 口型归零、淡出气泡 |
| `agent_command` | `action`(move/look/interact/wander), `target`{x,y,z}, `object_id` | **主动探索指令** |
| `agent_thought` | `text` | 小念“内心独白”（不念出声，想法气泡） |

## 4. 世界感知与主动探索（核心设计）

- **预加载 / 只感知已加载范围**：`world_state.py` 维护 `loaded_regions`；来自未加载区域的
  符号感知直接丢弃。小念和玩家一样，走到哪、加载到哪，只看见范围内东西。
- **符号感知无图像**：`SymbolicPerception.cs` 遍历场景推结构化文本+坐标；Python 端从不接触像素。
- **低频视觉快照 + 符号联合推理**：`visual_snapshot` 推 1080p 图，Python 端 `bridge.py`
  把「当前符号感知文本」注入视觉 prompt，让视觉模型**结合符号**理解画面（非盲看）。
- **建立在意识模型上**：符号/视觉文本都回写意识层 `clayer`（think + learn_async），
  长进联想图；`explorer.py` 每次决策都先 `mind.think(symbolic_text)` 取意识状态，再用
  「新颖度 + 类型兴趣 + 距离」打分挑目标，向 Unity 下发 `agent_command` —— 真正的主动，
  **不被动等待玩家交互**。
- 玩家一开口对话，探索自动让位（与屏幕正反馈同策略），互不抢资源。

## 5. 相关配置（.env）
| 键 | 默认 | 说明 |
|---|---|---|
| `WORLD_AUTONOMY_ENABLED` | true | 世界感知+主动探索总开关 |
| `WORLD_VISION_ENABLED` | true | 是否对视觉快照做视觉推理 |
| `WORLD_EXPLORE_INTERVAL` | 3.0 | 探索决策间隔(秒) |
| `WORLD_VISION_MAX_WIDTH` | 1280 | 视觉快照送入 API 前压缩宽度(1080p→降 token) |
| `WORLD_INTEREST_RADIUS` | 12.0 | 主动探索兴趣半径(米) |
| `WORLD_MOVE_SPEED` | 2.5 | 探索移动速度(米/秒，仅告知 Unity 参考) |
| `VISION_*` | — | 视觉 API 配置（同屏幕视觉） |

## 6. 你（项目方）要补的
- **预加载接线**：把你的场景流式加载/卸载接到 `SymbolicPerception.ReportRegion(regionId, loaded)`。
- **物体标注**：给可感知物体挂 `PerceptTag`（填 `id/name/type`）。
- **动画状态机**：建 `isMoving` 布尔、`interact` 触发器、`jump/turn/wave/pat/nod` 触发器。
- **VRM0 vs VRM1**：本客户端用 VRM0 的 `BlendShapePreset`；若用 VRM1(UniVRM 的 VRM10)，
  表情接口改为 `Vrm10Instance.LookAt` / `Vrm10Instance.Expression`，映射逻辑同构。
