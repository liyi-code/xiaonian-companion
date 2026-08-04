# 我的世界村庄式自给自足小镇 —— Unity 2022 + 小念大脑 整合说明

## 一、从 GitHub 获取的 skill
- **Unity-Skills**（https://github.com/Besty0728/Unity-Skills，MIT，1.5k★）
  - AI 驱动的 Unity 编辑器自动化引擎，通过 REST API 让 AI 直接操控 Unity
    （建物体/场景/材质/Animator/NavMesh/Terrain 等 776 个 skill）。
  - 已克隆到 `vendor_unity_skills/`，客户端复制到 `unity_client/unity_skills/`。
  - 要求 Unity **2022.3+**（你已装 2022.3.62 @ `D:\unity b\Editor`，满足）。

## 二、架构
```
Python 端（本地 Ollama 大脑）                 Unity 端（Unity 2022）
┌──────────────────────────┐  WebSocket     ┌──────────────────────────┐
│ src.bridge.py            │ ◄─────────────►│ BridgeHub.cs             │
│   ├─ NPCBrain × 6        │  town_state    │   ├─ 路由到 NpcAgent     │
│   │   (小念+5村民)        │  town_task     │   └─ 全局事件→TownView   │
│   ├─ src.town.py         │  town_contribute│ NpcAgent.cs             │
│   │   (经济模拟/自给自足) │ ─────────────► │   ├─ 对话/表情/动作/语音 │
│   └─ src.villagers.py    │ ◄───────────── │   └─ 接 town_task→去建筑 │
│       (职业预设)          │  town_event    │ TownView.cs（村庄面板）  │
│ src.assistant.py         │               │ TownLayout.cs（建筑坐标）│
│   └─ set_persona(职业)   │               │ AgentController.cs（移动）│
└──────────────────────────┘               └──────────────────────────┘
        ↑                                          ↑
   xiaoyue.py（小跃陪伴，本地 Ollama qwen2.5:14b）  Unity-Skills 客户端
                                               build_village.py（一键搭场景）
```

## 三、运行步骤
### A. Python 大脑（必做）
```
venv\Scripts\python.exe -m src.bridge --port 8765
```
- 启动后自动 spawn 小念+5村民进小镇，TownSim 每 5 秒 tick 一次。
- 实时把 `town_state` / `town_task` 广播给所有连接的 Unity 客户端。
- 想只看小镇状态（不依赖 Unity）：`venv\Scripts\python.exe run_town.py`

### B. Unity 场景（用 skill 自动搭建）
1. 用 Unity 2022 打开 `unity_project/` 工程（已放好全部 C# 脚本）。
2. 装 Unity-Skills 插件：
   - `Window > Package Manager > + > Add via Git URL`：
     `https://github.com/Besty0728/Unity-Skills.git?path=/SkillsForUnity`
3. 启动 Server：`Window > UnitySkills`，点顶部开关（默认 `http://localhost:8090`）。
4. 保持 Unity 打开，运行：
   `venv\Scripts\python.exe unity_client/unity_skills/build_village.py`
   → 自动建地形/7 栋建筑/6 个 NPC 挂载点(挂 NpcAgent+AgentController)/灯光/NavMesh。
5. 把小念/村民的 VRM 拖到对应 `NPC_*` 挂载点即可运行。

### C. 一键（可选）
`启动小镇_完整.bat` —— 先起大脑，等你确认 Unity Server 起来后自动搭场景。

## 四、小镇生态（自给自足循环）
- **6 职业 → 6 资源**：农夫(小麦) / 樵夫(木头) / 矿工(石头) / 厨师(食物) /
  商人(工具) / 铁匠(铁矿)。
- **采集→加工→消费链**：农夫种麦→厨师用麦+柴做饭喂全村；商人用木+石造工具→
  其它职业消耗工具生产；铁匠炼铁升级工具。
- **短缺自愈**：任意资源跌破安全线 → TownSim 自动给对应职业发 `town_task`
  （如木头空→樵夫去森林）→ Unity 侧 NpcAgent 走到建筑 → 上报 `town_event`
  → Python 补资源、关任务。闭环。
- **自给自足判定**：所有资源 ≥ 安全线 = ✅，否则 ⚠ 持续派任务直到恢复。

## 五、当前进度
- ✅ Python：town.py / villagers.py / bridge 集成 / assistant.set_persona / xiaoyue(本地Ollama)
- ✅ Unity：BridgeHub / NpcAgent(town_task闭环) / TownView(面板) / TownLayout / 全部脚本
- ✅ Unity-Skills 客户端 + build_village.py 一键搭场景
- ✅ 独立 unity_project/ 工程（Unity 2022 可直接打开）
- ⏳ 待你侧：装插件 + 启 Server + 拖 VRM（VRM 模型在你本地）

## 六、注意
- 本机 Unity 是 2022.3.62，满足 skill 要求。
- NpcAgent 用 UniVRM 的 `BlendShapeProxy`（VRM0）；若用 VRM1 需改 `Expression` API。
- 语音为字节/本地 SoVITS 合成字节下行，Unity 侧 WavUtil 解码播放。
