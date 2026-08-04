// NpcAgent.cs
// 单个 NPC 的全部「对话 / 表情 / 动作 / 语音 / 任务」行为。对应一个小念 VRM 实例。
// 下行事件由 BridgeHub 按 npc_id 路由到这里；上行（玩家输入/感知）由本脚本发起。
//
// 依赖：UniVRM（BlendShapeProxy 控制面部表情）。若用 VRM1，请把 SetBlendShape 内改成
//       runtime.Expression.SetWeight(...)。
using System;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using UnityEngine;
using UnityEngine.Networking;

#if UNITY_VRM
using VRM;
#endif

public class NpcAgent : MonoBehaviour
{
    [HideInInspector] public string npcId;
    [HideInInspector] public string displayName;

    [Header("UI（可选，挂 Canvas 上的气泡 Text）")]
    public TMPro.TextMeshProUGUI bubbleText;     // 对话气泡
    public float bubbleHoldSec = 4f;

    private AgentController _ctrl;                // 移动/看/交互（已有脚本）
    private AudioSource _audio;
    private float _bubbleTimer;

    // 小镇采集任务：当前要去的目标建筑位置（世界坐标），到达即上报生产完成
    private Vector3? _townTaskTarget;
    private string _townTaskObjId;

#if UNITY_VRM
    private BlendShapeProxy _bsProxy;
#endif

    // 表情维度 → BlendShape 预设名（VRM0 标准预设）
    private static readonly Dictionary<string, BlendShapePreset> _emoMap = new Dictionary<string, BlendShapePreset>
    {
        { "happy",     BlendShapePreset.Joy },
        { "joy",       BlendShapePreset.Joy },
        { "sad",       BlendShapePreset.Sorrow },
        { "sorrow",    BlendShapePreset.Sorrow },
        { "angry",     BlendShapePreset.Anger },
        { "anger",     BlendShapePreset.Anger },
        { "surprised", BlendShapePreset.Surprise },
        { "surprise",  BlendShapePreset.Surprise },
        { "neutral",   BlendShapePreset.Neutral },
    };

    public void Init(string id, string name, GameObject root)
    {
        npcId = id;
        displayName = name;
        _ctrl = GetComponent<AgentController>();
        _audio = GetComponent<AudioSource>();
        if (_audio == null) _audio = gameObject.AddComponent<AudioSource>();
#if UNITY_VRM
        _bsProxy = GetComponent<BlendShapeProxy>();
#endif
        BridgeHub.Instance?.RegisterAgent(this);
        gameObject.name = "NPC_" + name + "_" + id;
    }

    void OnDestroy() { BridgeHub.Instance?.UnregisterAgent(npcId); }

    // ---------------- 下行事件入口（BridgeHub 调用）----------------
    public void HandleEvent(JObject ev)
    {
        string type = (string)ev["type"];
        switch (type)
        {
            case "token":        AppendBubble((string)ev["text"]); break;
            case "emotion":      SetEmotion((string)ev["dominant"]); break;
            case "action":       PlayAction((string)ev["name"]); break;
            case "speech_start": StartSpeech(); break;
            case "audio":        PlayAudio((string)ev["wav"]); break;
            case "talk_stop":    StopSpeech(); break;
            case "agent_command": ApplyAgentCommand(ev); break;
            case "agent_thought": Debug.Log($"[{displayName}] 想法: {(string)ev["thought"]}"); break;
            case "quest_update":  QuestSystem.Instance?.OnQuestUpdate(npcId, ev); break;
            case "town_task":     OnTownTask(ev); break;   // 小镇采集任务
            default: Debug.Log($"[{displayName}] 未处理事件: {type}"); break;
        }
    }

    // ---------------- 对话气泡 ----------------
    private System.Text.StringBuilder _sb = new System.Text.StringBuilder();
    private void AppendBubble(string piece)
    {
        if (bubbleText == null) return;
        _sb.Append(piece);
        bubbleText.text = _sb.ToString();
        bubbleText.transform.parent?.gameObject.SetActive(true);
        _bubbleTimer = bubbleHoldSec;
    }
    private void StartSpeech()
    {
        _sb.Clear();
        if (bubbleText != null) bubbleText.text = "";
    }
    private void StopSpeech() { /* 气泡保留 _bubbleTimer 秒后隐藏 */ }

    void Update()
    {
        if (_bubbleTimer > 0)
        {
            _bubbleTimer -= Time.deltaTime;
            if (_bubbleTimer <= 0 && bubbleText != null)
                bubbleText.transform.parent?.gameObject.SetActive(false);
        }

        // 小镇任务：到达目标建筑 → 上报完成一轮生产
        if (_townTaskTarget != null)
        {
            float d = Vector3.Distance(transform.position, _townTaskTarget.Value);
            if (d < 1.5f)
            {
                TownView town = FindObjectOfType<TownView>();
                town?.ReportTownEvent(npcId, _townTaskObjId);
                Debug.Log($"[{displayName}] 到达 {_townTaskObjId}，上报生产完成");
                _townTaskTarget = null;
                _townTaskObjId = null;
            }
        }
    }

    // ---------------- 表情（面部 BlendShape）----------------
    private void SetEmotion(string dominant)
    {
        if (string.IsNullOrEmpty(dominant)) return;
#if UNITY_VRM
        if (_bsProxy == null) return;
        // 先清零常用表情，再置目标
        foreach (var kv in _emoMap)
            _bsProxy.ImmediatelySetValue(BlendShapeKey.CreateFromPreset(kv.Value), 0f);
        if (_emoMap.TryGetValue(dominant.ToLowerInvariant(), out var preset))
            _bsProxy.ImmediatelySetValue(BlendShapeKey.CreateFromPreset(preset), 1f);
#else
        Debug.Log($"[{displayName}] 表情(未启用VRM): {dominant}");
#endif
    }

    // ---------------- 动作 ----------------
    private void PlayAction(string name)
    {
        // 调用已有 AgentController 的动画触发；若没有则走 Animator
        if (_ctrl != null) { _ctrl.TriggerAction(name); return; }
        var anim = GetComponent<Animator>();
        if (anim != null) anim.SetTrigger(name);
    }

    // ---------------- 语音（解码 wav 播放）----------------
    private void PlayAudio(string b64)
    {
        if (string.IsNullOrEmpty(b64) || _audio == null) return;
        try
        {
            byte[] wav = Convert.FromBase64String(b64);
            var clip = WavUtil.ToAudioClip(wav);   // 见 WavUtil.cs（小工具）
            _audio.clip = clip;
            _audio.Play();
        }
        catch (Exception ex) { Debug.LogError($"[{displayName}] 音频解码失败: {ex.Message}"); }
    }

    // ---------------- 主动探索命令（来自 explorer）----------------
    private void ApplyAgentCommand(JObject ev)
    {
        if (_ctrl == null) return;
        string action = (string)ev["action"];
        var pos = ev["pos"];
        Vector3? target = pos != null
            ? new Vector3((float?)pos["x"] ?? 0, (float?)pos["y"] ?? 0, (float?)pos["z"] ?? 0)
            : (Vector3?)null;
        string objId = (string)ev["target"];
        _ctrl.HandleCommand(action, target, objId);
    }

    // ---------------- 小镇采集任务 ----------------
    private void OnTownTask(JObject ev)
    {
        string target = (string)ev["objective"]?["target"];
        if (string.IsNullOrEmpty(target)) return;
        // 在小镇建筑坐标表里找目标位置（与 Python src/town.py BUILDINGS 对齐）
        Vector3? pos = TownLayout.PosOf(target);
        if (pos == null)
        {
            Debug.LogWarning($"[{displayName}] 未知建筑目标: {target}");
            return;
        }
        _townTaskObjId = target;
        _townTaskTarget = pos;
        if (_ctrl != null)
            _ctrl.HandleCommand("move", pos, target);
        Debug.Log($"[{displayName}] 接到小镇任务，前往: {target} @ {pos}");
    }

    // ---------------- 上行：玩家输入 ----------------
    public void SendChat(string text)
    {
        BridgeHub.Instance?.SendUserInput(npcId, text);
    }
}
