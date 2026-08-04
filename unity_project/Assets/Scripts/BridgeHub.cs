// BridgeHub.cs
// 小念 ⇄ 3D 游戏 的「单一 WebSocket 连接 + 多 NPC 路由」中枢。
// 所有 NPC 共用一条 WebSocket；每条下行消息带 npc_id，本类把它分派给对应 NpcAgent。
// 上行消息（玩家输入/感知/任务事件）统一由 NpcAgent 经本类发出，自动带 npc_id。
//
// 依赖：System.Net.WebSockets（Unity 内置，无需第三方包）+ Newtonsoft.Json。
// 挂法：场景里放一个空 GameObject，挂本脚本；Inspector 填 wsUrl（默认 ws://127.0.0.1:8765）。
using System;
using System.Collections.Generic;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using Newtonsoft.Json.Linq;
using UnityEngine;

public class BridgeHub : MonoBehaviour
{
    [Header("连接")]
    public string wsUrl = "ws://127.0.0.1:8765";

    public static BridgeHub Instance { get; private set; }

    private ClientWebSocket _ws;
    private CancellationTokenSource _cts;
    private readonly Dictionary<string, NpcAgent> _agents = new Dictionary<string, NpcAgent>();
    private NpcManager _manager;

    public event Action OnConnected;
    public event Action<List<NpcInfo>> OnReady;   // 连接后 Python 告知的 NPC 列表

    // 小镇（我的世界村庄式自给自足）全局事件 —— town_state / town_task 不带 npc_id，全局广播
    public event Action<JObject> OnTownState;
    public event Action<JObject> OnTownTask;

    void Awake()
    {
        Instance = this;
        _manager = FindObjectOfType<NpcManager>();
    }

    void Start() { Connect(); }

    void OnDestroy() { Disconnect(); }

    // ---------------- 连接 ----------------
    public async void Connect()
    {
        try
        {
            _cts = new CancellationTokenSource();
            _ws = new ClientWebSocket();
            await _ws.ConnectAsync(new Uri(wsUrl), _cts.Token);
            Debug.Log("[BridgeHub] 已连接小念大脑");
            OnConnected?.Invoke();
            _ = ReceiveLoop();
        }
        catch (Exception e)
        {
            Debug.LogError("[BridgeHub] 连接失败: " + e.Message);
        }
    }

    private async System.Threading.Tasks.Task ReceiveLoop()
    {
        var buf = new byte[8192];
        try
        {
            while (_ws != null && _ws.State == WebSocketState.Open && !_cts.IsCancellationRequested)
            {
                var seg = new ArraySegment<byte>(buf);
                var res = await _ws.ReceiveAsync(seg, _cts.Token);
                if (res.MessageType == WebSocketMessageType.Close) break;
                string json = Encoding.UTF8.GetString(buf, 0, res.Count);
                // 切回主线程处理 UI/场景对象
                UnityMainThreadDispatcher.Enqueue(() => Route(json));
            }
        }
        catch (OperationCanceledException) { }
        catch (Exception e)
        {
            Debug.LogWarning("[BridgeHub] 接收异常: " + e.Message);
        }
    }

    public void Disconnect()
    {
        _cts?.Cancel();
        if (_ws != null)
        {
            try
            {
                if (_ws.State == WebSocketState.Open)
                    _ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "", CancellationToken.None).ConfigureAwait(false);
            }
            catch { }
            _ws.Dispose();
            _ws = null;
        }
    }

    public bool IsOpen => _ws != null && _ws.State == WebSocketState.Open;

    // ---------------- 下行路由 ----------------
    private void Route(string json)
    {
        JObject ev;
        try { ev = JObject.Parse(json); }
        catch (Exception ex) { Debug.LogError("[BridgeHub] JSON 解析失败: " + ex.Message); return; }

        string type = (string)ev["type"];
        string npcId = (string)ev["npc_id"];

        switch (type)
        {
            case "ready":
                var list = new List<NpcInfo>();
                foreach (var n in ev["npcs"] ?? new JArray())
                    list.Add(new NpcInfo { npcId = (string)n["npc_id"], name = (string)n["name"],
                                           voiceReady = (bool?)n["voice_ready"] ?? false });
                OnReady?.Invoke(list);
                _manager?.OnReady(list);
                break;

            case "npc_spawned":
                _manager?.SpawnAgent((string)ev["npc_id"], (string)ev["name"]);
                break;

            case "npc_despawned":
                _manager?.DespawnAgent((string)ev["npc_id"]);
                break;

            // ---- 小镇全局广播（自给自足村庄）----
            case "town_state":
                OnTownState?.Invoke(ev);
                break;
            case "town_task":
                OnTownTask?.Invoke(ev);
                break;

            default:
                // 其它事件都按 npc_id 分派给对应 Agent
                if (!string.IsNullOrEmpty(npcId) && _agents.TryGetValue(npcId, out var agent))
                    agent.HandleEvent(ev);
                else if (string.IsNullOrEmpty(npcId))
                    Debug.LogWarning("[BridgeHub] 收到无 npc_id 的事件: " + type);
                break;
        }
    }

    // ---------------- 上行（NpcAgent 调用）----------------
    public async void Send(string npcId, string type, Action<JObject> fill = null)
    {
        if (!IsOpen) return;
        var o = new JObject { ["npc_id"] = npcId, ["type"] = type };
        fill?.Invoke(o);
        var bytes = Encoding.UTF8.GetBytes(o.ToString());
        try
        {
            await _ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text,
                                true, _cts.Token);
        }
        catch (Exception e)
        {
            Debug.LogWarning("[BridgeHub] 发送失败: " + e.Message);
        }
    }

    // ---------------- Agent 注册 ----------------
    public void RegisterAgent(NpcAgent agent) { _agents[agent.npcId] = agent; }
    public void UnregisterAgent(string npcId) { _agents.Remove(npcId); }

    public void SendUserInput(string npcId, string text) =>
        Send(npcId, "user_input", o => o["text"] = text);

    public void SendWorldLoad(string npcId, string regionId, bool loaded) =>
        Send(npcId, "world_load", o => { o["region_id"] = regionId; o["loaded"] = loaded; });

    public void SendSymbolicPercept(string npcId, JObject payload) =>
        Send(npcId, "symbolic_percept", o =>
        {
            o["agent_pos"] = payload["agent_pos"];
            o["objects"] = payload["objects"];
        });

    public void SendVisualSnapshot(string npcId, string camPos, string b64) =>
        Send(npcId, "visual_snapshot", o => { o["cam_pos"] = camPos; o["image_b64"] = b64; });

    public void SendQuestEvent(string npcId, string kind, string objectId = null, string npcFrom = null) =>
        Send(npcId, "quest_event", o =>
        {
            o["kind"] = kind;
            if (objectId != null) o["object_id"] = objectId;
            if (npcFrom != null) o["npc_id_from"] = npcFrom;
        });
}

[Serializable]
public class NpcInfo
{
    public string npcId;
    public string name;
    public bool voiceReady;
}
