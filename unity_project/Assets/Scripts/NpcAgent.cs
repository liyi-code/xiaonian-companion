// NpcAgent.cs
// 单个 NPC 的全部「对话 / 表情 / 动作 / 语音 / 任务」行为。对应一个小念 VRM 实例。
// 下行事件由 BridgeHub 按 npc_id 路由到这里；上行（玩家输入/感知）由本脚本发起。
//
// 依赖：UniVRM（BlendShapeProxy 控制面部表情）。若用 VRM1，请把 SetBlendShape 内改成
//       runtime.Expression.SetWeight(...)。
using System;
using System.Collections;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using UnityEngine;
using UnityEngine.Networking;

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

    // 表情维度 → ExpressionController 接受的键
    private static readonly HashSet<string> _emotions = new HashSet<string>
    {
        "happy", "joy", "sad", "sorrow", "angry", "anger",
        "surprised", "surprise", "neutral", "calm", "anxiety"
    };

    public void Init(string id, string name, GameObject root)
    {
        npcId = id;
        displayName = name;
        _ctrl = GetComponent<AgentController>();
        _audio = GetComponent<AudioSource>();
        if (_audio == null) _audio = gameObject.AddComponent<AudioSource>();
        BridgeHub.Instance?.RegisterAgent(this);
        gameObject.name = "NPC_" + name + "_" + id;
    }

    void OnDestroy() { BridgeHub.Instance?.UnregisterAgent(npcId); }

    public void SendChat(string text)
    {
        if (string.IsNullOrEmpty(text)) return;
        BridgeHub.Instance?.SendUserInput(npcId, text);
    }

    // ---------------- 下行事件入口（BridgeHub 调用）----------------
    public void HandleEvent(JObject ev)
    {
        string type = (string)ev["type"];
        switch (type)
        {
            // ---- 文本 ----
            case "token":        AppendBubble((string)ev["text"]); break;
            case "chat":         AppendBubble((string)ev["text"]); break;  // 完整回复

            // ---- 情绪 ----
            case "emotion":
                {
                    var vec = ev["vector"] as JObject;
                    if (vec != null)
                        SetEmotionVector(vec);       // 5 维全向量
                    else
                        SetEmotion((string)ev["dominant"]);  // 兼容 legacy dominant 字段
                    break;
                }

            // ---- 动作（修正：走 ConceptStateMachine，全参 speed/amp/trait/lean）----
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
                        PlayAction(name, dur);         // 回退：无 CSM 时用旧 Animator
                    break;
                }

            // ---- 自发动作意图（补）----
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
                    break;
                }

            // ---- 意识层概念（补）----
            case "concepts":
                {
                    var items = ev["items"] as JArray;
                    var csm = GetComponent<ConceptStateMachine>();
                    if (csm != null && items != null)
                    {
                        foreach (var it in items)
                        {
                            var jt = it as JObject;
                            if (jt != null)
                            {
                                bool primary = jt.Value<bool?>("primary") ?? false;
                                if (primary)
                                    csm.TriggerConcept(
                                        (string)jt["name"],
                                        jt.Value<float?>("weight") ?? 0f);
                            }
                        }
                    }
                    break;
                }

            // ---- 躁动度（补）----
            case "restlessness":
                {
                    float v = ev.Value<float?>("value") ?? 0.2f;
                    var csm = GetComponent<ConceptStateMachine>();
                    if (csm != null) csm.SetRestlessness(v);
                    break;
                }

            // ---- 工具调用结果（补）----
            case "tool":
                Debug.Log($"[{displayName}] 工具 {(string)ev["name"]}: {(string)ev["result"]}");
                break;

            // ---- 语音 ----
            case "speech_start": StartSpeech(); break;
            case "audio":        PlayAudio((string)ev["wav"]); break;
            case "talk_stop":    StopSpeech(); break;

            // ---- 探索/想法 ----
            case "agent_command": ApplyAgentCommand(ev); break;
            case "agent_thought": Debug.Log($"[{displayName}] 想法: {(string)ev["thought"]}"); break;

            // ---- 小镇/任务 ----
            case "quest_update":  QuestSystem.Instance?.OnQuestUpdate(npcId, ev); break;
            case "town_task":     OnTownTask(ev); break;

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
        string key = dominant.ToLowerInvariant();
        var expr = GetComponent<ExpressionController>();
        if (expr != null)
        {
            if (_emotions.Contains(key))
                expr.ApplyEmotion(key, 1f);
            else
                expr.ResetAll();
        }
        else
        {
            Debug.Log($"[{displayName}] 表情(未挂ExpressionController): {dominant}");
        }
    }

    /// <summary>5 维情绪全向量 → ExpressionController（用于 emotion 消息含 vector 字段时）</summary>
    private void SetEmotionVector(JObject vec)
    {
        var expr = GetComponent<ExpressionController>();
        if (expr == null) return;
        float joy     = vec.Value<float?>("joy")     ?? 0f;
        float anger   = vec.Value<float?>("anger")   ?? 0f;
        float sadness = vec.Value<float?>("sadness") ?? 0f;
        float calm    = vec.Value<float?>("calm")    ?? 0f;
        float anxiety = vec.Value<float?>("anxiety") ?? 0f;
        expr.ApplyEmotion("joy",       joy);
        expr.ApplyEmotion("angry",     anger);
        expr.ApplyEmotion("sad",       sadness);
        expr.ApplyEmotion("neutral",   calm);
        expr.ApplyEmotion("surprised", anxiety);
    }

    // ---------------- 动作 ----------------
    private Coroutine _actionCoroutine;

    private void PlayAction(string name, float duration = 0f)
    {
        if (string.IsNullOrEmpty(name)) return;

        var anim = GetComponentInChildren<Animator>();
        if (anim != null)
        {
            // Python 端用简单名 wave，Animator 里 trigger/state 名可能是 ACT_WAVE
            string triggerName = name;
            if (!triggerName.StartsWith("ACT_", StringComparison.OrdinalIgnoreCase))
                triggerName = "ACT_" + triggerName.ToUpperInvariant();
            // 先触发 Animator 中同名 trigger（兼容旧状态机）
            anim.SetTrigger(triggerName);
            // 再强制播放同名状态，避免状态机里动作被立即切回 idle 只动 1 帧
            anim.Play(triggerName, 0, 0f);

            _currentAction = triggerName;
            if (duration > 0f)
            {
                if (_actionCoroutine != null) StopCoroutine(_actionCoroutine);
                _actionCoroutine = StartCoroutine(HoldAction(duration));
            }
        }

        // 移动/交互类命令仍交给 AgentController；动画类命令上面已处理
        if (_ctrl != null) _ctrl.HandleCommand(name, null, displayName);
    }

    private string _currentAction;

    private IEnumerator HoldAction(float duration)
    {
        yield return new WaitForSeconds(duration);
        var anim = GetComponentInChildren<Animator>();
        if (anim != null)
        {
            // 时间到后平滑切回 Idle；若状态机没有 Idle，ResetTrigger 兜底
            try { anim.CrossFade("Idle", 0.25f, 0); } catch { }
            if (!string.IsNullOrEmpty(_currentAction))
                anim.ResetTrigger(_currentAction);
        }
        _currentAction = null;
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

}
