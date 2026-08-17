// BridgeHub.cs
// 小念 ⇄ 3D 游戏 的「单一 WebSocket 连接 + 多 NPC 路由」中枢。
// 所有 NPC 共用一条 WebSocket；每条下行消息带 npc_id，本类把它分派给对应 NpcAgent。
// 上行消息（玩家输入/感知/任务事件）统一由 NpcAgent 经本类发出，自动带 npc_id。
//
// 依赖：WebSocketSharp（WebSocket 客户端）+ Newtonsoft.Json（JSON 解析）。
// 挂法：场景里放一个空 GameObject，挂本脚本；Inspector 填 wsUrl（默认 ws://127.0.0.1:8765）。
using System;
using System.Collections.Generic;
using WebSocketSharp;
using Newtonsoft.Json.Linq;
using UnityEngine;

public class BridgeHub : MonoBehaviour
{
    [Header("连接")]
    public string wsUrl = "ws://127.0.0.1:8765";

    public static BridgeHub Instance { get; private set; }

    private WebSocket _ws;
    private readonly Dictionary<string, NpcAgent> _agents = new Dictionary<string, NpcAgent>();
    private NpcManager _manager;

    public event Action OnConnected;
    public event Action<List<NpcInfo>> OnReady;   // 连接后 Python 告知的 NPC 列表

    void Awake()
    {
        Instance = this;
        _manager = FindObjectOfType<NpcManager>();
    }

    void Start() { Connect(); }

    void OnDestroy() { _ws?.Close(); }

    // ---------------- 连接 ----------------
    public void Connect()
    {
        _ws = new WebSocket(wsUrl);
        _ws.OnOpen += (s, e) =>
        {
            Debug.Log("[BridgeHub] 已连接小念大脑");
            OnConnected?.Invoke();
        };
        _ws.OnMessage += (s, e) => { if (e.IsText) Route(e.Data); };
        _ws.OnError += (s, e) => Debug.LogError("[BridgeHub] WS 错误: " + e.Message);
        _ws.OnClose += (s, e) => Debug.LogWarning("[BridgeHub] WS 关闭: " + e.Code);
        _ws.ConnectAsync();
    }

    public bool IsOpen => _ws != null && _ws.ReadyState == WebSocketState.Open;

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
    public void Send(string npcId, string type, Action<JObject> fill = null)
    {
        if (!IsOpen) return;
        var o = new JObject { ["npc_id"] = npcId, ["type"] = type };
        fill?.Invoke(o);
        _ws.Send(o.ToString());
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
}

[Serializable]
public class NpcInfo
{
    public string npcId;
    public string name;
    public bool voiceReady;
}
