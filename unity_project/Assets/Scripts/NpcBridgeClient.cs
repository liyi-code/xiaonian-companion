// NpcBridgeClient.cs
// 挂在每个 NPC（VRM 角色根物体）上。作为 WebSocket 客户端连接 Python bridge
// (ws://127.0.0.1:8765)，接收 token/emotion/concepts/action/audio，并分发给
// 子物体的 ExpressionController（表情）与 ConceptStateMachine（动作/状态机）。
//
// 协议（与 src/bridge.py 对齐）：
//   {"type":"ready"}                                   <- 服务端欢迎
//   {"type":"npc","id":..,"name":..}                  <- 分配 NPC 身份
//   {"type":"token","text":..} / {"type":"chat","text":..}  <- 文本（打字机/气泡）
//   {"type":"emotion","vector":{"joy":..,"anger":..,"sadness":..,"calm":..,"anxiety":..},"dominant":..}
//   {"type":"concepts","items":[{"name":..,"weight":..,"primary":..}],"entropy":..}
//   {"type":"action","name":..}                       <- 动作关键词
//   {"type":"action_intent","action":"[ACT_SIT]","prob":0.82}  <- 自发动作意图（无 LLM）
//   {"type":"speech_start"} / {"type":"talk_stop"}
//   {"type":"audio","wav":"<base64>"}                 <- WAV 字节流（Base64）
//
// 发送（玩家侧）：
//   {"type":"user_input","npc_id":<本NPC id>,"text":..}
//   {"type":"stimuli","npc_id":..,"stimuli":["玩家接近","社交"],"weight":0.9}
//   {"type":"get_spontaneous_action","npc_id":..,"context":["晚上","椅子"],"threshold":0.15}
//   {"type":"action_feedback","npc_id":..,"action":"[ACT_SIT]","context":[],"success":true}

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;
using UnityEngine.Events;
using UnityEngine.Networking;

[Serializable]
[RequireComponent(typeof(NpcBodyCollider))]
public class NpcBridgeClient : MonoBehaviour
{
    [Header("连接")]
    public string wsUrl = "ws://127.0.0.1:8765";
    public string npcId = "default";          // 与 Python spawn_npc 的 id 对应
    public string playerName = "玩家";

    [Header("引用（可选，自动查找子物体）")]
    public ExpressionController expression;   // 表情控制器（VRM BlendShape）
    public ConceptStateMachine stateMachine;  // 概念/动作状态机
    public UnityEngine.UI.Text subtitleText; // 头顶字幕（若无则禁用打字机）

    [Header("自发动作")]
    [Tooltip(">0 时，空闲状态下每隔这么多秒请求一次自发动作")]
    public float spontaneousActionInterval = 5f;
    [Tooltip("默认环境上下文；运行时可用 SetActionContext 覆盖")]
    public List<string> defaultActionContext = new List<string> { "空闲" };
    [Tooltip("收到动作意图时触发，参数为意图码如 [ACT_SIT]")]
    public UnityEvent<string> onActionIntent;

    private ClientWebSocket _ws;
    private CancellationTokenSource _cts;
    private bool _connected;

    public bool IsConnected => _connected;

    // 打字机缓冲
    private string _pendingText = "";
    private float _typeTimer;
    private const float TYPE_INTERVAL = 0.03f;

    // 自发动作
    private float _actionTimer;
    private List<string> _actionContext = new List<string>();
    private string _lastActionIntent = "";
    private List<string> _lastActionContext = new List<string>();

    void Start()
    {
        // npcId 默认从物体名推导：NPC_xiaonian -> xiaonian
        if (string.IsNullOrEmpty(npcId) || npcId == "default")
        {
            if (gameObject.name.StartsWith("NPC_"))
                npcId = gameObject.name.Substring(4);
            else
                npcId = gameObject.name;
        }

        // 防重复：同一个 GameObject 上出现多个 NpcBridgeClient 时只保留一个
        var all = GetComponents<NpcBridgeClient>();
        if (all.Length > 1)
        {
            int myIndex = Array.IndexOf(all, this);
            bool otherHasRealId = false;
            for (int i = 0; i < all.Length; i++)
            {
                if (i == myIndex) continue;
                if (!string.IsNullOrEmpty(all[i].npcId) && all[i].npcId != "default")
                    otherHasRealId = true;
            }
            if (otherHasRealId && (string.IsNullOrEmpty(npcId) || npcId == "default"))
            {
                Debug.LogWarning($"[NpcBridge:{npcId}] 检测到重复组件，销毁本组件");
                Destroy(this);
                return;
            }
            if (myIndex > 0 && !otherHasRealId)
            {
                Debug.LogWarning($"[NpcBridge:{npcId}] 检测到重复组件，保留第一个");
                Destroy(this);
                return;
            }
        }

        if (expression == null) expression = GetComponentInChildren<ExpressionController>();
        if (stateMachine == null) stateMachine = GetComponentInChildren<ConceptStateMachine>();
        if (stateMachine == null) stateMachine = gameObject.AddComponent<ConceptStateMachine>();

        ConnectAsync();
    }

    async void ConnectAsync()
    {
        try
        {
            // 多个 NPC 同时连同一端口容易触发 Aborted，错开 0~900ms
            await Task.Delay(Math.Abs(npcId.GetHashCode()) % 900);

            _ws = new ClientWebSocket();
            _cts = new CancellationTokenSource();
            await _ws.ConnectAsync(new Uri(wsUrl), _cts.Token);
            _connected = true;
            Debug.Log($"[NpcBridge:{npcId}] 已连接 {wsUrl}");
            // 告诉 Python 我是谁（触发 spawn_npc）
            SendJson(new Dictionary<string, object> { { "type", "hello" }, { "npc_id", npcId } });
            // 收消息循环：正确处理分段消息（音频/长文本不会越界）
            var buf = new byte[8192];
            while (_ws.State == WebSocketState.Open && !_cts.IsCancellationRequested)
            {
                using (var ms = new MemoryStream())
                {
                    WebSocketReceiveResult res;
                    do
                    {
                        var seg = new ArraySegment<byte>(buf);
                        res = await _ws.ReceiveAsync(seg, _cts.Token);
                        if (res.MessageType == WebSocketMessageType.Close) break;
                        ms.Write(buf, 0, res.Count);
                    } while (!res.EndOfMessage);

                    if (res.MessageType == WebSocketMessageType.Close) break;
                    string msg = Encoding.UTF8.GetString(ms.ToArray());
                    // 回到主线程处理（WebSocket 回调在 IO 线程）
                    UnityMainThreadDispatcher.Enqueue(() => OnMessage(msg));
                }
            }
        }
        catch (Exception e)
        {
            Debug.LogWarning($"[NpcBridge:{npcId}] 连接失败：{e.Message}");
        }
        _connected = false;
    }

    void OnMessage(string json)
    {
        try
        {
            var doc = MiniJSON.Json.Deserialize(json) as Dictionary<string, object>;
            if (doc == null) return;
            string type = doc["type"] as string;
            Debug.Log($"[NpcBridge:{npcId}] << 收到消息 type={type} npc_id={(doc.ContainsKey("npc_id") ? doc["npc_id"] : "无")} (本机npcId={npcId})");
            // 躁动度(restlessness)：Python 可在任意消息携带，驱动呼吸/转头频率
            // （无聊→低，等待/期待→高）。叠加层永远生效，独立于具体动作。
            if (doc.ContainsKey("restlessness") && stateMachine != null)
            {
                try { stateMachine.SetRestlessness(Convert.ToSingle(doc["restlessness"])); }
                catch { }
            }
            // 只处理发给自己的消息（广播类型如 ready 没有 npc_id，放行）
            if (doc.ContainsKey("npc_id") && doc["npc_id"] is string targetId && targetId != npcId)
            {
                Debug.LogWarning($"[NpcBridge:{npcId}] 丢弃非本机消息: 收到 npc_id={targetId}, 本机={npcId} (type={type})");
                return;
            }
            switch (type)
            {
                case "token":
                case "chat":
                    string incoming = (doc["text"] as string) ?? "";
                    _pendingText += incoming;
                    Debug.Log($"[NpcBridge:{npcId}] 收到回复: {incoming}");
                    if (stateMachine != null) stateMachine.OnSpeechStart();
                    break;
                case "emotion":
                    if (expression != null && doc.ContainsKey("vector"))
                    {
                        var v = doc["vector"] as Dictionary<string, object>;
                        expression.SetEmotion(
                            ToFloat(v, "joy"), ToFloat(v, "anger"),
                            ToFloat(v, "sadness"), ToFloat(v, "calm"),
                            ToFloat(v, "anxiety"));
                    }
                    break;
                case "concepts":
                    if (stateMachine != null && doc.ContainsKey("items"))
                    {
                        var items = doc["items"] as List<object>;
                        foreach (var it in items)
                        {
                            var d = it as Dictionary<string, object>;
                            bool primary = d.ContainsKey("primary") && Convert.ToBoolean(d["primary"]);
                            if (primary) stateMachine.TriggerConcept(d["name"] as string,
                                                                     ToFloat(d, "weight"));
                        }
                    }
                    break;
                case "action":
                    {
                        string name = doc["name"] as string;
                        float dur = 0f;
                        float speed = 1f;
                        float amplitude = 1f;
                        string trait = "";
                        float lean = 0f;
                        if (doc.ContainsKey("duration"))
                            dur = Convert.ToSingle(doc["duration"]);
                        if (doc.ContainsKey("speed"))
                            speed = Convert.ToSingle(doc["speed"]);
                        if (doc.ContainsKey("amplitude"))
                            amplitude = Convert.ToSingle(doc["amplitude"]);
                        if (doc.ContainsKey("trait"))
                            trait = doc["trait"] as string ?? "";
                        if (doc.ContainsKey("lean"))
                            lean = Convert.ToSingle(doc["lean"]);
                        Debug.LogError($"[NpcBridge:{npcId}] ★收到 action 事件: name={name} dur={dur} speed={speed} amp={amplitude} trait={trait} lean={lean} stateMachine={(stateMachine!=null)}");
                        if (stateMachine != null) stateMachine.TriggerAction(name, dur, speed, amplitude, trait, lean);
                        else Debug.LogWarning($"[NpcBridge:{npcId}] stateMachine 为 null，无法播放动作 {name}");
                        break;
                    }
                case "speech_start":
                    if (stateMachine != null) stateMachine.OnSpeechStart();
                    break;
                case "talk_stop":
                    if (stateMachine != null) stateMachine.OnSpeechStop();
                    break;
                case "audio":
                    string b64 = doc["wav"] as string;
                    PlayAudioBase64(b64);
                    break;
                case "action_intent":
                    string action = (doc["action"] as string) ?? "[ACT_IDLE]";
                    _lastActionIntent = action;
                    _lastActionContext = GetList(doc, "context");
                    string probStr = doc.ContainsKey("prob") ? doc["prob"].ToString() : "?";
                    Debug.Log($"[NpcBridge:{npcId}] 动作意图: {action} (prob={probStr})");
                    try
                    {
                        float dur = 0f, spd = 1f, amp = 1f, lean = 0f;
                        string trait = "";
                        if (doc.ContainsKey("duration")) dur = Convert.ToSingle(doc["duration"]);
                        if (doc.ContainsKey("speed")) spd = Convert.ToSingle(doc["speed"]);
                        if (doc.ContainsKey("amplitude")) amp = Convert.ToSingle(doc["amplitude"]);
                        if (doc.ContainsKey("trait")) trait = doc["trait"] as string ?? "";
                        if (doc.ContainsKey("lean")) lean = Convert.ToSingle(doc["lean"]);
                        if (stateMachine != null) stateMachine.TriggerAction(action, dur, spd, amp, trait, lean);
                    }
                    catch (Exception ex)
                    {
                        Debug.LogWarning($"[NpcBridge:{npcId}] TriggerAction 失败：{ex.Message}");
                    }
                    onActionIntent?.Invoke(action);
                    break;
            }
        }
        catch (Exception e)
        {
            Debug.LogWarning($"[NpcBridge:{npcId}] 解析消息失败：{e.Message}");
        }
    }

    // ---- 本地关键词动作（与 Python 的 detect_action 保持一致）----
    private string LocalDetectAction(string text)
    {
        if (string.IsNullOrEmpty(text)) return null;
        var t = text.ToLowerInvariant();
        var greetings = new string[] {
            "你好", "您好", "早上好", "晚上好", "中午好", "下午好", "晚安",
            "在吗", "在干嘛", "嗨", "哈喽", "hello", "hi", "拜拜", "再见",
            "挥手", "招手", "打招呼", "摇手", "拜"
        };
        foreach (var g in greetings)
            if (t.Contains(g)) return "ACT_WAVE";
        var turns = new string[] { "转身", "转过去", "转个身", "环顾", "看四周", "左右看" };
        foreach (var g in turns)
            if (t.Contains(g)) return "ACT_LOOKAROUND";
        var follows = new string[] { "跟", "跟着我", "过来", "跟上来" };
        foreach (var g in follows)
            if (t.Contains(g)) return "ACT_FOLLOW";
        var sits = new string[] { "坐", "坐下", "休息一下", "歇会儿" };
        foreach (var g in sits)
            if (t.Contains(g)) return "ACT_SIT";
        var stand = new string[] { "立正", "站好", "站直", "别动", "停", "停下" };
        foreach (var g in stand)
            if (t.Contains(g)) return "ACT_STAND";
        return null; // 普通聊天：不本地预演，等 Python 发 ACT_NOD 等反应
    }

    // ---- 发送 ----
    public void SendChat(string text)
    {
        // 本地即时反馈：玩家一发消息，角色立刻给一个轻量反应（点头），
        // 不依赖 Python 链路，即使后端卡顿也有“输入有反应”。
        // Python 端会按语义下发更精确的动作（打招呼→挥手/转身→环顾等），
        // TriggerAction 支持打断，不会冲突。
        if (stateMachine != null)
        {
            var localAction = LocalDetectAction(text);
            if (localAction != null)
            {
                // 问候类用较快速度(兴奋)，普通动作速度 1
                float spd = (localAction == "ACT_WAVE") ? 1.0f : 1.0f;
                stateMachine.TriggerAction(localAction, 1.5f, spd, 1.0f);
            }
            else
                stateMachine.TriggerAction("ACT_NOD", 1.0f, 0.9f, 0.8f); // 普通聊天：慢而轻的点头
        }
        else
            Debug.LogWarning($"[NpcBridge:{npcId}] SendChat 时 stateMachine 为 null");
        SendJson(new Dictionary<string, object> {
            { "type", "user_input" }, { "npc_id", npcId }, { "text", text } });
    }

    public void SendStimuli(List<string> stimuli, float weight)
    {
        SendJson(new Dictionary<string, object> {
            { "type", "stimuli" }, { "npc_id", npcId },
            { "stimuli", stimuli }, { "weight", weight } });
    }

    /// <summary>
    /// 设置下一次自发动作请求使用的环境上下文（如时间、地点、玩家状态）。
    /// 可由外部感知脚本每帧调用，驱动意识层形成环境与动作的联想。
    /// </summary>
    public void SetActionContext(List<string> context)
    {
        _actionContext = context != null ? new List<string>(context) : new List<string>();
    }

    /// <summary>
    /// 立即向 Python 请求一次自发动作；不传 context 时使用 defaultActionContext。
    /// </summary>
    public void RequestSpontaneousAction(List<string> context = null)
    {
        var ctx = (context != null && context.Count > 0)
            ? context
            : (_actionContext.Count > 0 ? _actionContext : defaultActionContext);
        SendJson(new Dictionary<string, object> {
            { "type", "get_spontaneous_action" },
            { "npc_id", npcId },
            { "context", ctx },
            { "threshold", 0.15 }
        });
    }

    /// <summary>
    /// 报告动作执行结果：成功则强化环境-动作联想，失败则弱化，避免对着空气重复执行。
    /// </summary>
    public void ReportActionFeedback(bool success)
    {
        SendJson(new Dictionary<string, object> {
            { "type", "action_feedback" },
            { "npc_id", npcId },
            { "action", _lastActionIntent },
            { "context", _lastActionContext },
            { "success", success }
        });
    }

    void SendJson(Dictionary<string, object> obj)
    {
        if (!_connected || _ws == null)
        {
            string t = obj.ContainsKey("type") ? obj["type"] as string : "?";
            Debug.LogWarning($"[NpcBridge:{npcId}] 未连接，无法发送 {t}");
            return;
        }
        string s = MiniJSON.Json.Serialize(obj);
        var bytes = Encoding.UTF8.GetBytes(s);
        if (s.Length < 200) Debug.Log($"[NpcBridge:{npcId}] → {s}");
        else Debug.Log($"[NpcBridge:{npcId}] → {s.Substring(0, 80)} ... ({s.Length} 字符)");
        _ = SendJsonAsync(obj); // fire-and-forget，异常在内部捕获
    }

    async Task SendJsonAsync(Dictionary<string, object> obj)
    {
        if (!_connected || _ws == null) return;
        try
        {
            string s = MiniJSON.Json.Serialize(obj);
            var bytes = Encoding.UTF8.GetBytes(s);
            await _ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text,
                                true, _cts.Token);
        }
        catch (Exception e)
        {
            Debug.LogWarning($"[NpcBridge:{npcId}] 发送失败：{e.Message}");
        }
    }

    // ---- 音频播放 ----
    void PlayAudioBase64(string b64)
    {
        try
        {
            byte[] wav = Convert.FromBase64String(b64);
            StartCoroutine(LoadAndPlay(wav));
        }
        catch (Exception e) { Debug.LogWarning($"[NpcBridge] 音频解码失败：{e.Message}"); }
    }

    System.Collections.IEnumerator LoadAndPlay(byte[] wav)
    {
        // 用 UnityWebRequest 把 WAV 字节加载为 AudioClip（支持内存流）
        using var req = UnityWebRequestMultimedia.GetAudioClip(
            "data:audio/wav;base64," + Convert.ToBase64String(wav), AudioType.WAV);
        yield return req.SendWebRequest();
        if (req.result == UnityWebRequest.Result.Success)
        {
            var clip = DownloadHandlerAudioClip.GetContent(req);
            var src = GetComponent<AudioSource>();
            if (src == null) src = gameObject.AddComponent<AudioSource>();
            src.clip = clip;
            src.Play();
            if (stateMachine != null) stateMachine.OnAudioPlay(src);
        }
        else Debug.LogWarning($"[NpcBridge] 音频加载失败：{req.error}");
    }

    // ---- 打字机更新 ----
    void Update()
    {
        if (!string.IsNullOrEmpty(_pendingText) && subtitleText != null)
        {
            _typeTimer += Time.deltaTime;
            if (_typeTimer >= TYPE_INTERVAL)
            {
                _typeTimer = 0;
                int n = Mathf.Min(2, _pendingText.Length);
                subtitleText.text += _pendingText.Substring(0, n);
                _pendingText = _pendingText.Substring(n);
            }
        }

        // 空闲时定时请求自发动作（纯意识层图计算，不调用 LLM，很快）
        if (_connected && spontaneousActionInterval > 0)
        {
            _actionTimer += Time.deltaTime;
            if (_actionTimer >= spontaneousActionInterval)
            {
                _actionTimer = 0;
                RequestSpontaneousAction();
            }
        }
    }

    void OnApplicationQuit()
    {
        // 避坑：优雅关闭，通知 Python 做记忆整合
        SendJson(new Dictionary<string, object> { { "type", "shutdown" }, { "npc_id", npcId } });
        try { _cts?.Cancel(); _ws?.CloseAsync(WebSocketCloseStatus.NormalClosure, "", default); }
        catch { }
    }

    static float ToFloat(Dictionary<string, object> d, string k)
    {
        if (d != null && d.ContainsKey(k) && d[k] is double f) return (float)f;
        return 0f;
    }

    static List<string> GetList(Dictionary<string, object> d, string k)
    {
        var res = new List<string>();
        if (d == null || !d.ContainsKey(k)) return res;
        if (d[k] is List<object> list)
        {
            foreach (var o in list)
                if (o != null) res.Add(o.ToString());
        }
        return res;
    }
}
