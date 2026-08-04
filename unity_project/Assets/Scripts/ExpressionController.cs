// ExpressionController.cs
// 把 Python 推来的 5 维情绪（joy/anger/sadness/calm/anxiety）映射到 UniVRM 1.0 的
// ExpressionPreset（happy/angry/sad/relaxed/surprised/neutral）。
//
// 本文件使用反射访问 UniVRM 1.0，避免 Unity Package Manager 没正确加载 VRM10 包时
// 出现编译错误。只要运行时 VRM10 程序集存在，表情仍然可以生效。

using System;
using System.Reflection;
using UnityEngine;

public class ExpressionController : MonoBehaviour
{
    private Component _vrm;
    private object _expressionRuntime;
    private MethodInfo _setWeightMethod;
    private Type _expressionPresetType;

    [Range(0f, 1f)] public float smooth = 0.15f;

    private float _joy, _anger, _sadness, _calm, _anxiety;

    void Awake()
    {
        ResolveVrm();
    }

    void ResolveVrm()
    {
        // 1) 在已加载程序集里找 UniVRM 1.0 的 Vrm10Instance 类型
        Type vrmType = FindType("uniVRM10.Vrm10Instance");
        if (vrmType == null)
        {
            Debug.LogWarning("[Expression] 未找到 Vrm10Instance 类型（UniVRM 1.0 未加载），表情将不可用。");
            return;
        }

        // 2) 拿到 GameObject 上的 Vrm10Instance 组件
        _vrm = GetComponent(vrmType) as Component;
        if (_vrm == null) _vrm = GetComponentInChildren(vrmType) as Component;
        if (_vrm == null)
        {
            Debug.LogWarning("[Expression] 未找到 Vrm10Instance 组件，表情将不可用。");
            return;
        }

        // 3) 反射取 Runtime 属性
        PropertyInfo runtimeProp = vrmType.GetProperty("Runtime", BindingFlags.Public | BindingFlags.Instance);
        if (runtimeProp == null) return;
        object runtime = runtimeProp.GetValue(_vrm);
        if (runtime == null) return;

        // 4) 反射取 Runtime.Expression 属性
        Type runtimeType = runtime.GetType();
        PropertyInfo expressionProp = runtimeType.GetProperty("Expression", BindingFlags.Public | BindingFlags.Instance);
        if (expressionProp == null) return;
        _expressionRuntime = expressionProp.GetValue(runtime);
        if (_expressionRuntime == null) return;

        // 5) 找 ExpressionPreset 枚举和 SetWeight 方法
        _expressionPresetType = FindType("uniVRM10.ExpressionPreset");
        if (_expressionPresetType == null) return;

        Type exprType = _expressionRuntime.GetType();
        _setWeightMethod = exprType.GetMethod(
            "SetWeight",
            BindingFlags.Public | BindingFlags.Instance,
            null,
            new Type[] { _expressionPresetType, typeof(float) },
            null
        );
    }

    static Type FindType(string fullName)
    {
        Type t = Type.GetType(fullName);
        if (t != null) return t;

        foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
        {
            t = asm.GetType(fullName);
            if (t != null) return t;
        }
        return null;
    }

    /// <summary>由 NpcBridgeClient 调用：设置 5 维情绪（每个 0~1）。</summary>
    public void SetEmotion(float joy, float anger, float sadness, float calm, float anxiety)
    {
        _joy = Mathf.Clamp01(joy);
        _anger = Mathf.Clamp01(anger);
        _sadness = Mathf.Clamp01(sadness);
        _calm = Mathf.Clamp01(calm);
        _anxiety = Mathf.Clamp01(anxiety);
    }

    /// <summary>兼容旧版 NpcAgent 的调用：用字符串键触发单个表情。</summary>
    public void ApplyEmotion(string key, float weight)
    {
        switch (key.ToLowerInvariant())
        {
            case "happy":
            case "joy": SetEmotion(weight, 0, 0, 0, 0); break;
            case "angry":
            case "anger": SetEmotion(0, weight, 0, 0, 0); break;
            case "sad":
            case "sorrow": SetEmotion(0, 0, weight, 0, 0); break;
            case "calm":
            case "relaxed": SetEmotion(0, 0, 0, weight, 0); break;
            case "surprised":
            case "surprise":
            case "anxiety": SetEmotion(0, 0, 0, 0, weight); break;
            case "neutral":
            default: SetEmotion(0, 0, 0, 1 - weight, 0); break;
        }
    }

    public void ResetAll()
    {
        SetEmotion(0, 0, 0, 1f, 0);
    }

    void Update()
    {
        if (_vrm == null || _expressionRuntime == null || _setWeightMethod == null)
            return;

        float calmFactor = 1f - 0.6f * _calm;
        TrySet("happy", _joy * calmFactor);
        TrySet("angry", _anger * calmFactor);
        TrySet("sad", _sadness * calmFactor);
        TrySet("relaxed", _calm);
        TrySet("surprised", _anxiety * calmFactor);
    }

    void TrySet(string presetName, float w)
    {
        try
        {
            object value = Enum.Parse(_expressionPresetType, presetName, true);
            _setWeightMethod.Invoke(_expressionRuntime, new object[] { value, w });
        }
        catch (Exception e)
        {
            Debug.LogWarning($"[Expression] SetWeight({presetName}, {w}) 失败：{e.Message}");
        }
    }
}
