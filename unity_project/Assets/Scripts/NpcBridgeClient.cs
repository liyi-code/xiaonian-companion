// NpcBridgeClient.cs
// 挂在每个 NPC（VRM 角色根物体）上。负责初始化 ConceptStateMachine / ExpressionController，
// 提供本地关键词动作检测(LocalDetectAction)与音频播放，以及 NPC 身份注册。
// ★ WebSocket 通信已统一迁移到 BridgeHub + NpcAgent 架构，本脚本不再持有独立连接。
// 协议（与 src/bridge.py 对齐）由 BridgeHub.cs 与 NpcAgent.cs 处理。
// ============================================================================
using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Events;
using UnityEngine.Networking;

[Serializable]
[RequireComponent(typeof(NpcBodyCollider))]
public class NpcBridgeClient : MonoBehaviour
{
    [Header("连接")]
    public string npcId = "default";          // 与 Python spawn_npc 的 id 对应
    public string playerName = "玩家";

    [Header("引用（可选，自动查找子物体）")]
    public ExpressionController expression;   // 表情控制器（VRM BlendShape）
    public ConceptStateMachine stateMachine;  // 概念/动作状态机

    [Header("自发动作")]
    [Tooltip("收到动作意图时触发，参数为意图码如 [ACT_SIT]")]
    public UnityEvent<string> onActionIntent;

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
        if (stateMachine == null) stateMachine = gameObject.AddComponent<ConceptStateMachine>();

        PrintReadyLog();
    }

    /// <summary>确保 NPC 被 BridgeHub 注册（供外部在需要时调用）</summary>
    public void EnsureRegistered()
    {
        // 同时注册 NpcAgent（如果物体上尚未挂载）
        var agent = GetComponent<NpcAgent>();
        if (agent == null)
        {
            agent = gameObject.AddComponent<NpcAgent>();
            agent.Init(npcId, playerName, gameObject);
        }
        BridgeHub.Instance?.RegisterAgent(agent);
    }

    private void PrintReadyLog()
    {
        Debug.Log($"[NpcBridge:{npcId}] 初始化完成 (CSM={(stateMachine!=null)} Expr={(expression!=null)}) — WS 由 BridgeHub 管理");
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


    // ---- 发送（统一走 BridgeHub 单连接）----
    public void SendChat(string text)
    {
        if (string.IsNullOrEmpty(text)) return;
        // 本地即时反馈：玩家一发消息，角色立刻给一个轻量反应（点头）
        if (stateMachine != null)
        {
            var localAction = LocalDetectAction(text);
            if (localAction != null)
                stateMachine.TriggerAction(localAction, 1.5f, 1.0f, 1.0f);
            else
                stateMachine.TriggerAction("ACT_NOD", 1.0f, 0.9f, 0.8f);
        }
        BridgeHub.Instance?.SendUserInput(npcId, text);
    }

    public void RequestSpontaneousAction(List<string> context = null)
    {
        var ctx = context ?? defaultActionContext;
        BridgeHub.Instance?.SendRequest(npcId, "get_spontaneous_action", ctx);
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

    private List<string> defaultActionContext = new List<string> { "空闲" };
}
