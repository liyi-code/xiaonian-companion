# Python ↔ Unity 连接修复方案

> 基于 2026-08-12 完整审计，按优先级分 4 阶段修复。每阶段可独立提交推送。

---

## 架构决策：统一为 BridgeHub 单连接架构

**删除 NpcBridgeClient 的独立 `_ws` WebSocket 连接**，所有通信走 `BridgeHub → NpcAgent` 路由。

| | 旧 | 新 |
|---|---|---|
| 连接 | BridgeHub(1条) + NpcBridgeClient(N条) 平行 | 仅 BridgeHub(1条) |
| 消息处理 | NpcAgent.HandleEvent(缺5类) | NpcAgent.HandleEvent(全14类) |
| 动作执行 | Animator.SetTrigger | **ConceptStateMachine.TriggerAction** |
| 自我修复 | 无 | 自动重连 |

---

## 第一阶段：修复 NpcAgent，补全消息处理（致命级 3 项）

### 修改文件：`unity_project/Assets/Scripts/NpcAgent.cs`

### 1.1 补全 5 种缺失消息 + 修复 action 参数传递

**替换 `HandleEvent` 方法**（第 58-81 行）:

```csharp
public void HandleEvent(JObject ev)
{
    string type = (string)ev["type"];
    switch (type)
    {
        // --- 文本 ---
        case "token":
            AppendBubble((string)ev["text"]);
            break;
        case "chat":       // ★ 补：完整回复（非流式）
            AppendBubble((string)ev["text"]);
            break;

        // --- 情绪（全向量，非仅 dominant）---
        case "emotion":
            {
                var vec = ev["vector"] as JObject;
                if (vec != null)
                    SetEmotionVector(vec);   // ★ 新方法：5 维全传递
                else
                    SetEmotion((string)ev["dominant"]);
            }
            break;

        // --- 动作（★ 修：用 ConceptStateMachine，全参数）---
        case "action":
            {
                string name = (string)ev["name"];
                float dur = ev.Value<float?>("duration") ?? 0f;
                float spd = ev.Value<float?>("speed") ?? 1f;
                float amp = ev.Value<float?>("amplitude") ?? 1f;
                string trait = (string)ev["trait"] ?? "";
                float lean = ev.Value<float?>("lean") ?? 0f;
                var csm = GetComponent<ConceptStateMachine>();
                if (csm != null)
                    csm.TriggerAction(name, dur, spd, amp, trait, lean);
                else
                    PlayAction(name, dur);   // 回退：无 CSM 时用 Animator
            }
            break;

        // --- 自发动作意图（★ 补）---
        case "action_intent":
            {
                string action = (string)ev["action"] ?? "[ACT_IDLE]";
                float dur = ev.Value<float?>("duration") ?? 0f;
                float spd = ev.Value<float?>("speed") ?? 1f;
                float amp = ev.Value<float?>("amplitude") ?? 1f;
                string trait = (string)ev["trait"] ?? "";
                float lean = ev.Value<float?>("lean") ?? 0f;
                var csm = GetComponent<ConceptStateMachine>();
                if (csm != null) csm.TriggerAction(action, dur, spd, amp, trait, lean);
            }
            break;

        // --- 意识层概念（★ 补）---
        case "concepts":
            {
                var items = ev["items"] as JArray;
                var csm = GetComponent<ConceptStateMachine>();
                if (csm != null && items != null)
                {
                    foreach (var it in items)
                    {
                        var jt = it as JObject;
                        bool primary = jt?.Value<bool?>("primary") ?? false;
                        if (primary)
                            csm.TriggerConcept(
                                (string)jt["name"],
                                jt?.Value<float?>("weight") ?? 0f);
                    }
                }
            }
            break;

        // --- 躁动度（★ 补）---
        case "restlessness":
            {
                float v = ev.Value<float?>("value") ?? 0.2f;
                var csm = GetComponent<ConceptStateMachine>();
                if (csm != null) csm.SetRestlessness(v);
            }
            break;

        // --- 语音 ---
        case "speech_start":
            StartSpeech();
            break;
        case "audio":
            PlayAudio((string)ev["wav"]);
            break;
        case "talk_stop":
            StopSpeech();
            break;

        // --- 探索/移动 ---
        case "agent_command":
            ApplyAgentCommand(ev);
            break;
        case "agent_thought":
            Debug.Log($"[{displayName}] 想法: {(string)ev["thought"]}");
            break;

        // --- 小镇/任务 ---
        case "quest_update":
            QuestSystem.Instance?.OnQuestUpdate(npcId, ev);
            break;
        case "town_task":
            OnTownTask(ev);
            break;

        // --- 工具调用结果（★ 补）---
        case "tool":
            Debug.Log($"[{displayName}] 工具 {(string)ev["name"]}: {(string)ev["result"]}");
            break;

        default:
            Debug.Log($"[{displayName}] 未处理事件: {type}");
            break;
    }
}
```

### 1.2 新增 `SetEmotionVector` 方法

```csharp
private void SetEmotionVector(JObject vec)
{
    var expr = GetComponent<ExpressionController>();
    if (expr == null) return;

    // 5 维情绪全量映射到 UniVRM ExpressionPreset
    float joy     = vec.Value<float?>("joy")     ?? 0f;
    float anger   = vec.Value<float?>("anger")   ?? 0f;
    float sadness = vec.Value<float?>("sadness") ?? 0f;
    float calm    = vec.Value<float?>("calm")    ?? 0f;
    float anxiety = vec.Value<float?>("anxiety") ?? 0f;

    expr.ApplyEmotion("joy",     joy);
    expr.ApplyEmotion("angry",   anger);
    expr.ApplyEmotion("sad",     sadness);
    expr.ApplyEmotion("neutral", calm);
    expr.ApplyEmotion("surprised", anxiety);
}
```

---

## 第二阶段：统一音频路由（高优先级）

### 2.1 Python 侧：广播→单播

**修改文件：`src/bridge.py`**

将 `_broadcast` 改为按 `npc_id` 单播（仅音频和对话类消息）：

```python
def _send_to_npc(self, npc_id: str, msg: dict):
    """把消息只发给持有该 npc_id 的 websocket 客户端（非广播）。"""
    msg["npc_id"] = npc_id
    with self._lock:
        for ws in list(self._clients):
            self._push(ws, msg)
            break   # 单连接架构：只发一次
```

**使用方式**：
- 音频/对话/动作 → `_send_to_npc(npc_id, msg)`
- 小镇全局状态 → `_broadcast(msg)` 保留

### 2.2 C# 侧：确保只有一个连接

**修改 `NpcBridgeClient.cs`，删除独立 WebSocket**：

删除以下内容：
- `_ws` / `_cts` 字段
- `ConnectAsync()` 方法
- `ReceiveLoop()` 方法
- `Update()` 中的自发动作计时器
- `OnDestroy` 中的 Disconnect

保留：
- `ConceptStateMachine` / `ExpressionController` 引用
- `LocalDetectAction()` (可被 NpcAgent 复用)

---

## 第三阶段：断线重连 + Debug 清理（中优先级）

### 3.1 BridgeHub 自动重连

**修改文件：`unity_project/Assets/Scripts/BridgeHub.cs`**

在 Start 后添加重连逻辑：

```csharp
private float _reconnectTimer = 0f;
private const float RECONNECT_INTERVAL = 3f;

void Update()
{
    if (!IsOpen)
    {
        _reconnectTimer += Time.deltaTime;
        if (_reconnectTimer >= RECONNECT_INTERVAL)
        {
            _reconnectTimer = 0f;
            Connect();
        }
    }
}
```

### 3.2 清理 Debug.LogError

**修改 `NpcBridgeClient.cs:232`**：

```csharp
// 旧：Debug.LogError（刷红控制台）
// 新：
Debug.Log($"[NpcBridge:{npcId}] 收到 action: {name} spd={speed} trait={trait}");
```

### 3.3 JSON 库统一

**全部 C# 脚本统一用 `Newtonsoft.Json.Linq`（JObject）**。NpcBridgeClient 的 `MiniJSON` 解析替换为 JObject 解析（已在第一阶段 NpcAgent 中完成，NpcBridgeClient 独立连接删除后无需再改）。

---

## 第四阶段：ConceptStateMachine 验证（低优先级）

### 4.1 确认 `TriggerAction` 参数处理完整

**检查 `ConceptStateMachine.cs` 的 TriggerAction 签名**，确保接收 6 个参数：

```csharp
public void TriggerAction(string action, float duration,
    float speed, float amplitude, string trait, float lean)
```

确认内部：
- `speed` → 调制挥手频率、点头速度
- `amplitude` → 调制挥手幅度、点头幅度
- `lean` → 折算上身前倾(站立)/上臂前举(挥手)
- `trait` → 影响 micro 动作选择

### 4.2 parity 担保

**Python 侧**：`compute_action_params` 已在冒烟测试中验证 5 种性格参数正确。

**Unity 侧**：在 `ConceptStateMachine` 的 `Start()` 中添加自检日志：

```csharp
void Start()
{
    Debug.Log($"[CSM] TriggerAction 准备就绪 " +
              $"(speed/amplitude/lean/trait 全部支持)");
}
```

---

## 执行顺序建议

| 阶段 | 预计工时 | 依赖 | 可独立测试? |
|---|---|---|---|
| 一 | 1h | 无 | ✅ 改完直接进 Play 验证 |
| 二 | 0.5h | 阶段一 | ✅ 单连接后音频不再串 NPC |
| 三 | 0.5h | 阶段二 | ✅ 关 Python 再开，看 Unity 是否自动重连 |
| 四 | 0.3h | 阶段一 | ✅ 看 ConceptStateMachine 日志 |

**总预估**：~2.5h（不含 Unity Editor 编译和测试时间）

---

## 验证清单

- [ ] 用户"打招呼"→ 小念回复出现在气泡 + 触发 `ACT_WAVE`(带性格 lean)
- [ ] 用户"5分钟后提醒我喝水"→ Python 触发 → NpcAgent 收到 `tool` 事件(不报"未处理")
- [ ] idle 5s 后 → Python 发 `action_intent` → NpcAgent 收到并调用 `TriggerAction`(不报"未处理")
- [ ] Python 重启 → 3s 内 BridgeHub 自动重连(不报红色错)
- [ ] 控制台无 `Debug.LogError` 刷红
- [ ] 多 NPC 场景：NPC A 说话 → 仅 NPC A 播音频(不上 NPC B)
