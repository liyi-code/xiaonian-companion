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
        // 3D 空间音：小念的声音从她身上发出、随距离衰减（自建语音通道）
        _audio.spatialBlend = 1f;
        _audio.minDistance = 0.5f;
        _audio.maxDistance = 25f;
        _audio.rolloffMode = AudioRolloffMode.Linear;
        BridgeHub.Instance?.RegisterAgent(this);
        gameObject.name = "NPC_" + name + "_" + id;
        EnsureBubbleUI();
    }

    void Start()
    {
        // 兜底：即便 Init 未被调用（场景里直接挂 NpcAgent），也保证气泡存在
        EnsureBubbleUI();
        // Init 可能在 BridgeHub.Awake 之前执行导致注册失败；
        // Start 阶段 Instance 必已就绪，这里补注册一次（幂等），否则桥的下行事件无法路由到本 NPC
        if (!string.IsNullOrEmpty(npcId))
            BridgeHub.Instance?.RegisterAgent(this);
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
            case "chat":         ShowBubble((string)ev["text"]); break;  // 完整回复：直接替换（避免与 token 流叠加重复）

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

            // ---- 动作库播放（学到的动捕动画）----
            case "play_clip":     PlayClip((string)ev["clip_path"], ev.Value<float?>("duration") ?? 0f); break;

            default: Debug.Log($"[{displayName}] 未处理事件: {type}"); break;
        }
    }

    // ---------------- 对话气泡 ----------------
    private System.Text.StringBuilder _sb = new System.Text.StringBuilder();
    private GameObject _bubbleRoot;                 // 自动生成的气泡根（含 WorldSpace Canvas）
    private UnityEngine.UI.Text _autoBubble;        // 自动生成的旧版动态字体 Text（中文友好，无需生成 TMP 中文字体）

    /// <summary>
    /// 运行时自动生成对话气泡（WorldSpace Canvas + 背景 + 旧版动态字体 Text）。
    /// Inspector 未挂 bubbleText（TMP）时兜底；旧版动态字体走系统字体回退，中文可直接显示。
    /// </summary>
    private void EnsureBubbleUI()
    {
        if (bubbleText != null)
        {
            _bubbleRoot = bubbleText.transform.parent != null
                ? bubbleText.transform.parent.gameObject : bubbleText.gameObject;
            return;
        }
        if (_bubbleRoot != null) return;

        var root = new GameObject("ChatBubble");
        root.transform.SetParent(transform, false);
        root.transform.localPosition = new Vector3(0f, 1.75f, 0f);
        var canvas = root.AddComponent<Canvas>();
        canvas.renderMode = RenderMode.WorldSpace;
        canvas.sortingOrder = 100;
        var crect = canvas.GetComponent<RectTransform>();
        crect.sizeDelta = new Vector2(480f, 120f);
        // WorldSpace Canvas 的 1 单位 = 1 米：缩放后气泡约 96cm 宽
        root.transform.localScale = new Vector3(0.002f, 0.002f, 0.002f);

        var bgGo = new GameObject("Bg");
        bgGo.transform.SetParent(root.transform, false);
        var img = bgGo.AddComponent<UnityEngine.UI.Image>();
        img.color = new Color(0.08f, 0.08f, 0.14f, 0.88f);
        img.raycastTarget = false;
        var bgRect = img.rectTransform;
        bgRect.anchorMin = Vector2.zero; bgRect.anchorMax = Vector2.one;
        bgRect.offsetMin = Vector2.zero; bgRect.offsetMax = Vector2.zero;

        var textGo = new GameObject("Text");
        textGo.transform.SetParent(root.transform, false);
        _autoBubble = textGo.AddComponent<UnityEngine.UI.Text>();
        var font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
        if (font == null) font = Resources.GetBuiltinResource<Font>("Arial.ttf");
        _autoBubble.font = font;
        _autoBubble.fontSize = 22;
        _autoBubble.alignment = TextAnchor.MiddleCenter;
        _autoBubble.horizontalOverflow = HorizontalWrapMode.Wrap;
        _autoBubble.verticalOverflow = VerticalWrapMode.Overflow;
        _autoBubble.color = Color.white;
        _autoBubble.raycastTarget = false;
        var tRect = _autoBubble.rectTransform;
        tRect.anchorMin = Vector2.zero; tRect.anchorMax = Vector2.one;
        tRect.offsetMin = new Vector2(10f, 6f); tRect.offsetMax = new Vector2(-10f, -6f);

        _bubbleRoot = root;
        root.SetActive(false);
    }

    private void SetBubbleText(string t)
    {
        if (bubbleText != null) bubbleText.text = t;
        if (_autoBubble != null) _autoBubble.text = t;
    }

    private void SetBubbleVisible(bool v)
    {
        if (bubbleText != null && bubbleText.transform.parent != null)
            bubbleText.transform.parent.gameObject.SetActive(v);
        if (_bubbleRoot != null) _bubbleRoot.SetActive(v);
    }

    private void AppendBubble(string piece)
    {
        if (bubbleText == null && _autoBubble == null) return;
        _sb.Append(piece);
        SetBubbleText(_sb.ToString());
        SetBubbleVisible(true);
        _bubbleTimer = bubbleHoldSec;
    }

    /// <summary>完整回复：直接替换气泡内容（桥在 token 流之后还会推一次完整 chat，避免重复叠加）</summary>
    private void ShowBubble(string text)
    {
        if (bubbleText == null && _autoBubble == null) return;
        _sb.Clear();
        if (!string.IsNullOrEmpty(text)) _sb.Append(text);
        SetBubbleText(text ?? "");
        SetBubbleVisible(true);
        _bubbleTimer = Mathf.Max(bubbleHoldSec, 8f);   // 完整回复多停留一会儿
    }

    private void StartSpeech()
    {
        // 只重置流式累积缓冲；不再清空气泡文本——TTS 的 speech_start 在完整 chat 之后才到达，
        // 清空会把已显示的回复抹掉（这正是「回复只在终端、Unity 无文字」的隐藏帮凶之一）
        _sb.Clear();
        SetBubbleVisible(true);
    }
    private void StopSpeech() { /* 气泡保留 _bubbleTimer 秒后隐藏 */ }

    void Update()
    {
        if (_bubbleTimer > 0)
        {
            _bubbleTimer -= Time.deltaTime;
            if (_bubbleTimer <= 0)
                SetBubbleVisible(false);
        }

        // 气泡永远面向玩家相机（公告板），避免从背后看文字镜像
        BillboardBubble();

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

    void BillboardBubble()
    {
        // 相机兜底：联机玩家相机打过 MainCamera 标签，但旧预制体/直开场景可能没有，
        // 找不到就退而求其次用任一启用中的相机，保证公告板永远有人脸可朝。
        var cam = Camera.main;
        if (cam == null)
        {
            var cams = FindObjectsOfType<Camera>();
            cam = System.Linq.Enumerable.FirstOrDefault(cams, c => c.enabled && c.gameObject.activeInHierarchy)
                  ?? (cams.Length > 0 ? cams[0] : null);
        }
        if (cam == null) return;
        // UI 画布的可见面是 -Z（与 Sprite/Quad 一致）：必须让 +Z 背对相机、-Z 朝向相机，
        // 文字才可读。若用 LookRotation(相机→物体) 让 +Z 朝相机，看到的正好是镜像面。
        Vector3 f = transform.position - cam.transform.position;
        f.y = 0f;
        if (f.sqrMagnitude < 0.001f) return;
        Quaternion rot = Quaternion.LookRotation(f);
        if (_bubbleRoot != null && _bubbleRoot.activeSelf)
            _bubbleRoot.transform.rotation = rot;
        if (bubbleText != null && bubbleText.transform.parent != null)
            bubbleText.transform.parent.rotation = rot;
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
            // 主机：把桥推来的小念语音转发给所有客户端（自建 3D 语音通道），
            // 远端各自在本机 NPC 上 3D 播放——人人都在她身边听到她。
            try { NetworkPlayerSync.BroadcastNpcAudio(wav); } catch (Exception) { }
        }
        catch (Exception ex) { Debug.LogError($"[{displayName}] 音频解码失败: {ex.Message}"); }
    }

    /// <summary>自建语音通道：远端客户端播放主机转来的小念语音（本机 NPC 上 3D 播放）。</summary>
    public void PlayWavBytes(byte[] wav)
    {
        if (wav == null || wav.Length == 0 || _audio == null) return;
        try
        {
            _audio.clip = WavUtil.ToAudioClip(wav);
            _audio.Play();
        }
        catch (Exception ex) { Debug.LogError($"[{displayName}] 远端音频播放失败: {ex.Message}"); }
    }

    // ---------------- 主动探索命令（来自 explorer）----------------
    private void ApplyAgentCommand(JObject ev)
    {
        if (_ctrl == null) return;
        string action = (string)ev["action"];
        // 协议兼容双读：
        // 标准为 pos(坐标字典) + target(物体id字符串)；
        // 旧版 explorer 曾发 target(坐标字典) + object_id，两种都兜住，避免 move/look 拿到空坐标
        var pos = ev["pos"] ?? (ev["target"] is JObject ? ev["target"] : null);
        Vector3? target = pos is JObject p
            ? new Vector3(p.Value<float?>("x") ?? 0f, p.Value<float?>("y") ?? 0f, p.Value<float?>("z") ?? 0f)
            : (Vector3?)null;
        string objId = (string)ev["target"];
        if (string.IsNullOrEmpty(objId)) objId = (string)ev["object_id"];
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

    // ---------------- 动作库播放（学到的动捕动画） ----------------
    private Coroutine _clipTimer;

    private void PlayClip(string path, float duration)
    {
        if (string.IsNullOrEmpty(path))
        {
            Debug.LogWarning($"[{displayName}] play_clip 缺少 clip_path");
            return;
        }
#if UNITY_EDITOR
        var clip = UnityEditor.AssetDatabase.LoadAssetAtPath<AnimationClip>(path);
        if (clip == null)
        {
            Debug.LogWarning($"[{displayName}] 找不到动画资源: {path}");
            return;
        }
        var anim = GetComponent<Animation>();
        if (anim == null) anim = gameObject.AddComponent<Animation>();
        anim.AddClip(clip, clip.name);
        anim.Play(clip.name);
        Debug.Log($"[{displayName}] 播放学到的动作: {path} ({(duration > 0f ? duration : clip.length):F2}s)");
        if (_clipTimer != null) StopCoroutine(_clipTimer);
        _clipTimer = StartCoroutine(StopClip(duration > 0f ? duration : clip.length));
#else
        Debug.LogWarning("[play_clip] 仅编辑器模式支持");
#endif
    }

    private System.Collections.IEnumerator StopClip(float d)
    {
        yield return new WaitForSeconds(Mathf.Max(0.5f, d));
        var anim = GetComponent<Animation>();
        if (anim != null) anim.Stop();
    }

}
