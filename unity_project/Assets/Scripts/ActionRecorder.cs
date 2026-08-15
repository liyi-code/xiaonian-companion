// ActionRecorder.cs —— 动捕教学间的「语音锚定录制器」
// ============================================================================
// 挂到表演者化身（humanoid Animator）根物体上，持续环形录制全部 Humanoid 骨骼。
// 当 Python 端检测到教学句（"我开心的时候你就做这个"）时，经 WebSocket 下发
// capture_action 事件，本脚本把最近 N 秒的动作切片成 AnimationClip 存为 .anim，
// 再回执 capture_result 给桥，由桥把 (动画, 教学句) 配对写进动作库。
//
// 用法：
//   1) 挂到表演者化身根物体；
//   2) 编辑器中按 C 键：手动录制最近 6 秒并保存（无桥也可测试）；
//   3) 与桥联动：桥下发 {"type":"capture_action","seconds":10}。
// 注意：使用 UnityEditor.GameObjectRecorder，仅编辑器模式（本项目无打包需求）。
// ============================================================================
using System;
using System.Collections;
using System.Collections.Generic;
using Newtonsoft.Json.Linq;
using UnityEngine;
#if UNITY_EDITOR
using UnityEditor;
using UnityEditor.Animations;
#endif

public class ActionRecorder : MonoBehaviour
{
    public static ActionRecorder Instance { get; private set; }

    [Header("录制")]
    [Tooltip("环形缓冲最大长度(秒)；录制超过后丢弃最旧帧")]
    public float ringSeconds = 20f;

    private Animator _anim;
#if UNITY_EDITOR
    private GameObjectRecorder _recorder;
#endif
    private readonly List<Transform> _bones = new List<Transform>();
    private float _recordedTime;

    void Awake()
    {
        Instance = this;
        _anim = GetComponentInChildren<Animator>();
        if (_anim == null || _anim.avatar == null || !_anim.avatar.isHuman)
        {
            Debug.LogWarning("[ActionRecorder] 需要挂在一个 humanoid 模型（有 Animator）上");
            enabled = false;
            return;
        }
        // 只绑定 Humanoid 标准骨骼 + 根（文件小、重定向稳），避免录进几十个非骨骼 Transform
        foreach (HumanBodyBones b in Enum.GetValues(typeof(HumanBodyBones)))
        {
            if (b == HumanBodyBones.LastBone) continue;   // LastBone 是枚举哨兵，不是真实骨骼
            var t = _anim.GetBoneTransform(b);
            if (t != null) _bones.Add(t);
        }
        if (_bones.Count == 0)
        {
            Debug.LogWarning("[ActionRecorder] 未取到任何 Humanoid 骨骼，录制不可用");
            enabled = false;
            return;
        }
        if (_anim.transform != _bones[0])
            _bones.Insert(0, _anim.transform);
#if UNITY_EDITOR
        _recorder = new GameObjectRecorder(gameObject);
        foreach (var bone in _bones)
            _recorder.BindComponentsOfType<Transform>(bone.gameObject, false);
#endif
        Debug.Log($"[ActionRecorder] 就绪：绑定 {_bones.Count} 根骨骼，环形 {ringSeconds}s");
    }

    void OnDestroy()
    {
        if (Instance == this) Instance = null;
    }

    void Update()
    {
#if UNITY_EDITOR
        if (_recorder != null)
        {
            _recorder.TakeSnapshot(Time.deltaTime);
            _recordedTime += Time.deltaTime;
            // 超过环形长度就重置（最简单的环形实现；切片只取尾部所以足够）
            if (_recordedTime > ringSeconds)
            {
                _recorder = new GameObjectRecorder(gameObject);
                foreach (var bone in _bones)
                    _recorder.BindComponentsOfType<Transform>(bone.gameObject, false);
                _recordedTime = 0f;
            }
        }
#endif
        // 编辑器手动测试：按 C 录最近 6 秒
        if (Input.GetKeyDown(KeyCode.C))
            CaptureAndSave(6f, "manual_" + DateTime.Now.ToString("HHmmss"));
    }

    /// <summary>把最近 seconds 秒的动作切成新 AnimationClip。返回 null 表示无数据。</summary>
    public AnimationClip CaptureLast(float seconds)
    {
#if UNITY_EDITOR
        if (_recorder == null) return null;
        var full = new AnimationClip();
        _recorder.SaveToClip(full);
        return TrimTail(full, Mathf.Min(seconds, ringSeconds));
#else
        return null;
#endif
    }

#if UNITY_EDITOR
    private AnimationClip TrimTail(AnimationClip full, float seconds)
    {
        // 全片时长以曲线最大键时间为准
        float endTime = 0f;
        foreach (var b in AnimationUtility.GetCurveBindings(full))
        {
            var c = AnimationUtility.GetEditorCurve(full, b);
            if (c != null && c.keys.Length > 0)
                endTime = Mathf.Max(endTime, c.keys[c.keys.Length - 1].time);
        }
        float start = Mathf.Max(0f, endTime - seconds);
        float len = endTime - start;
        if (len <= 0.02f)
        {
            Debug.LogWarning("[ActionRecorder] 录制时长不足，请先表演动作再触发");
            DestroyImmediate(full);
            return null;
        }

        var clip = new AnimationClip { frameRate = 60f };
        foreach (var b in AnimationUtility.GetCurveBindings(full))
        {
            var src = AnimationUtility.GetEditorCurve(full, b);
            if (src == null) continue;
            var keys = new List<Keyframe>();
            foreach (var k in src.keys)
            {
                if (k.time < start - 0.001f) continue;
                keys.Add(new Keyframe(k.time - start, k.value, k.inTangent, k.outTangent,
                                      k.inWeight, k.outWeight));
            }
            if (keys.Count == 0) continue;
            if (keys[0].time > 0.001f)
                keys.Insert(0, new Keyframe(0f, keys[0].value));
            var curve = new AnimationCurve(keys.ToArray());
            AnimationUtility.SetEditorCurve(clip, b, curve);
        }
        var settings = AnimationUtility.GetAnimationClipSettings(clip);
        settings.stopTime = len;
        settings.loopTime = false;
        AnimationUtility.SetAnimationClipSettings(clip, settings);
        clip.EnsureQuaternionContinuity();
        DestroyImmediate(full);
        return clip;
    }
#endif

    /// <summary>切片并保存为 Assets/Animations/<name>.anim。返回资源路径。</summary>
    public string CaptureAndSave(float seconds, string name)
    {
        var clip = CaptureLast(seconds);
        if (clip == null) return null;
#if UNITY_EDITOR
        const string dir = "Assets/Animations";
        if (!AssetDatabase.IsValidFolder(dir))
            AssetDatabase.CreateFolder("Assets", "Animations");
        string safe = System.Text.RegularExpressions.Regex.Replace(name ?? "taught", "[^0-9A-Za-z_\\-]", "_");
        string path = $"{dir}/{safe}.anim";
        path = AssetDatabase.GenerateUniqueAssetPath(path);
        AssetDatabase.CreateAsset(clip, path);
        AssetDatabase.SaveAssets();
        Debug.Log($"[ActionRecorder] 已保存动作: {path} ({(clip.length):F2}s)");
        return path;
#else
        return null;
#endif
    }

    /// <summary>处理桥下发的 capture_action 事件（由 BridgeHub.Route 调用）。</summary>
    public static void HandleCapture(JObject ev)
    {
        var rec = Instance;
        if (rec == null)
        {
            Debug.LogWarning("[ActionRecorder] 场景里没有 ActionRecorder 实例，无法录制");
            return;
        }
        float seconds = ev.Value<float?>("seconds") ?? 10f;
        string name = (string)ev["name"] ?? ("taught_" + DateTime.Now.ToString("HHmmss"));
        string path = rec.CaptureAndSave(seconds, name);
        float dur = 0f;
#if UNITY_EDITOR
        var clip = AssetDatabase.LoadAssetAtPath<AnimationClip>(path);
        dur = clip != null ? clip.length : 0f;
#endif
        // 回执给桥（配对教学句入库）
        BridgeHub.Instance?.Send("default", "capture_result", o =>
        {
            o["clip_path"] = path ?? "";
            o["duration"] = dur;
            o["name"] = name;
        });
    }
}
