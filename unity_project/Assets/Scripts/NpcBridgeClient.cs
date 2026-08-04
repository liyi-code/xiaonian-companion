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
using System.Linq;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using UnityEngine;
using UnityEngine.Events;
using UnityEngine.Networking;

[Serializable]
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
        if (expression == null) expression = GetComponentInChildren<ExpressionController>();
        if (stateMachine == null) stateMachine = GetComponentInChildren<ConceptStateMachine>();
        ConnectAsync();
    }

    async void ConnectAsync()
    {
        try
        {
            _ws = new ClientWebSocket();
            _cts = new CancellationTokenSource();
            await _ws.ConnectAsync(new Uri(wsUrl), _cts.Token);
            _connected = true;
            Debug.Log($"[NpcBridge:{npcId}] 已连接 {wsUrl}");
            // 告诉 Python 我是谁（触发 spawn_npc）
            SendJson(new Dictionary<string, object> { { "type", "hello" }, { "npc_id", npcId } });
            // 收消息循环
            var buf = new byte[8192];
            while (_ws.State == WebSocketState.Open && !_cts.IsCancellationRequested)
            {
                var seg = new ArraySegment<byte>(buf);
                var res = await _ws.ReceiveAsync(seg, _cts.Token);
                if (res.MessageType == WebSocketMessageType.Close) break;
                int len = res.Count;
                string msg = Encoding.UTF8.GetString(buf, 0, len);
                // 回到主线程处理（WebSocket 回调在 IO 线程）
                UnityMainThreadDispatcher.Enqueue(() => OnMessage(msg));
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
                    if (stateMachine != null) stateMachine.TriggerAction(doc["name"] as string);
                    break;
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
                        if (stateMachine != null) stateMachine.TriggerAction(action);
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

    // ---- 发送 ----
    public void SendChat(string text)
    {
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
        Debug.Log($"[NpcBridge:{npcId}] → {s}");
        try { _ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text,
                            true, _cts.Token); }
        catch (Exception e) { Debug.LogWarning($"[NpcBridge:{npcId}] 发送失败：{e.Message}"); }
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
