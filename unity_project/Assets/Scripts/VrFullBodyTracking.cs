// VrFullBodyTracking.cs —— VR 全身捕捉（OpenXR 通用，适配学校/未知型号头显）
// ============================================================================
// 挂在玩家根物体上（NetworkPlayerSync 会在运行时自动添加），VR 运行后自动接管化身：
//   · 头：贴到头显（相机由 NetworkPlayerSync 驱动）
//   · 手：左右手柄 → 手臂双骨 IK（手骨再贴手柄旋转）
//   · 髋 + 双脚（可选）：自动发现额外追踪器（如 Vive Tracker ×3 或身体追踪外设），
//     按 C 校准后：髋部贴髋追踪器、双腿 IK 到脚追踪器 —— 完整全身捕捉(FBT)
// 桌面模式（无头显）自动休眠；VR 下禁用 ConceptStateMachine 待机姿态，避免抢骨头。
// 所有 API 均为 OpenXR 通用（InputDevices），不依赖具体头显品牌。
// ============================================================================
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.XR;

public class VrFullBodyTracking : MonoBehaviour
{
    [Header("开关")]
    public bool autoEnable = true;              // 检测到 XR 自动启用
    public KeyCode calibrateKey = KeyCode.C;    // 站直后按：校准髋部偏移与腿长

    [Header("骨骼（留空则从 Humanoid Avatar 自动找）")]
    public Animator avatarAnimator;
    public Transform headBone, hipsBone;
    public Transform lHandBone, rHandBone, lFootBone, rFootBone;

    [Header("IK 弯曲方向（肘/膝朝哪边折；若反了就翻转向量）")]
    public Vector3 elbowHint = new Vector3(0f, -1f, 0f);   // 肘向下外
    public Vector3 kneeHint = new Vector3(0f, 0f, 1f);     // 膝向前

    Transform _lUpperArm, _lLowerArm, _rUpperArm, _rLowerArm;
    Transform _lUpperLeg, _lLowerLeg, _rUpperLeg, _rLowerLeg;

    readonly List<InputDevice> _trackers = new List<InputDevice>();
    InputDevice _hip, _leftFootDev, _rightFootDev;

    bool _vrActive;
    bool _calibrated;
    Vector3 _hipTrackerOffset;   // 髋追踪器 → 髋骨 的世界偏移（含身高差）
    float _legLength = 0.9f;
    ConceptStateMachine _csm;
    float _rescanTimer;

    void Start()
    {
        _csm = GetComponent<ConceptStateMachine>();
        if (avatarAnimator == null) avatarAnimator = GetComponentInChildren<Animator>();
        CacheBones();
    }

    void CacheBones()
    {
        if (avatarAnimator == null || avatarAnimator.avatar == null || !avatarAnimator.avatar.isHuman) return;
        if (headBone == null) headBone = avatarAnimator.GetBoneTransform(HumanBodyBones.Head);
        if (hipsBone == null) hipsBone = avatarAnimator.GetBoneTransform(HumanBodyBones.Hips);
        if (lHandBone == null) lHandBone = avatarAnimator.GetBoneTransform(HumanBodyBones.LeftHand);
        if (rHandBone == null) rHandBone = avatarAnimator.GetBoneTransform(HumanBodyBones.RightHand);
        if (lFootBone == null) lFootBone = avatarAnimator.GetBoneTransform(HumanBodyBones.LeftFoot);
        if (rFootBone == null) rFootBone = avatarAnimator.GetBoneTransform(HumanBodyBones.RightFoot);
        _lUpperArm = avatarAnimator.GetBoneTransform(HumanBodyBones.LeftUpperArm);
        _lLowerArm = avatarAnimator.GetBoneTransform(HumanBodyBones.LeftLowerArm);
        _rUpperArm = avatarAnimator.GetBoneTransform(HumanBodyBones.RightUpperArm);
        _rLowerArm = avatarAnimator.GetBoneTransform(HumanBodyBones.RightLowerArm);
        _lUpperLeg = avatarAnimator.GetBoneTransform(HumanBodyBones.LeftUpperLeg);
        _lLowerLeg = avatarAnimator.GetBoneTransform(HumanBodyBones.LeftLowerLeg);
        _rUpperLeg = avatarAnimator.GetBoneTransform(HumanBodyBones.RightUpperLeg);
        _rLowerLeg = avatarAnimator.GetBoneTransform(HumanBodyBones.RightLowerLeg);
    }

    void Update()
    {
        if (!autoEnable) return;

        // 周期性重扫：头显后插上也能自动启用
        _rescanTimer -= Time.deltaTime;
        if (!_vrActive)
        {
            if (_rescanTimer <= 0f)
            {
                _rescanTimer = 3f;
                _vrActive = XRSettings.isDeviceActive;
                if (_vrActive) OnVrEnabled();
            }
            return;
        }

        if (Input.GetKeyDown(calibrateKey))
            Calibrate();

        // 自动发现额外追踪器（髋/脚），没找齐就每 3 秒补扫
        if (_trackers.Count < 3)
            ScanExtraTrackers();
    }

    void OnVrEnabled()
    {
        // VR 接管：ConceptStateMachine 待机姿态让位（避免每帧抢头/手臂骨骼）
        if (_csm != null) _csm.enabled = false;
        ScanExtraTrackers();
        Debug.Log("[FBT] VR 全身捕捉已启用：头+双手生效；髋/脚追踪器自动发现中，站直后按 C 校准");
    }

    void ScanExtraTrackers()
    {
        var all = new List<InputDevice>();
        InputDevices.GetDevices(all);
        _trackers.Clear();
        var head = InputDevices.GetDeviceAtXRNode(XRNode.CenterEye);
        var lh = InputDevices.GetDeviceAtXRNode(XRNode.LeftHand);
        var rh = InputDevices.GetDeviceAtXRNode(XRNode.RightHand);
        foreach (var d in all)
        {
            if (!d.characteristics.HasFlag(InputDeviceCharacteristics.TrackedDevice)) continue;
            if (d == head || d == lh || d == rh) continue;
            if (!d.TryGetFeatureValue(CommonUsages.isTracked, out bool tr) || !tr) continue;
            _trackers.Add(d);
        }
        if (_trackers.Count > 0)
            Debug.Log($"[FBT] 发现 {_trackers.Count} 个额外追踪器（站直后按 C 校准分类：最低=脚、近头下=髋）");
    }

    void Calibrate()
    {
        _calibrated = true;
        if (_trackers.Count == 0)
        {
            Debug.LogWarning("[FBT] 没有额外追踪器 → 只有头+双手捕捉（无髋/腿）");
            return;
        }

        var pts = new List<(InputDevice d, Vector3 p)>();
        foreach (var t in _trackers)
            if (t.TryGetFeatureValue(CommonUsages.devicePosition, out Vector3 p))
                pts.Add((t, p));
        if (pts.Count == 0) { Debug.LogWarning("[FBT] 追踪器无位置数据"); return; }

        pts.Sort((a, b) => a.p.y.CompareTo(b.p.y));   // 按高度排
        if (pts.Count >= 3)
        {
            // 3 个：最低两个=脚（按 x 分左右），剩下=髋
            _rightFootDev = pts[0].d;
            _leftFootDev = pts[1].d;
            _hip = pts[2].d;
            if (_rightFootDev.TryGetFeatureValue(CommonUsages.devicePosition, out Vector3 rf) &&
                _leftFootDev.TryGetFeatureValue(CommonUsages.devicePosition, out Vector3 lf) &&
                rf.x < lf.x)
            {
                (_rightFootDev, _leftFootDev) = (_leftFootDev, _rightFootDev);
            }
        }
        else
        {
            // 不足 3 个：最低=右脚，最高=髋（尽力而为）
            _rightFootDev = pts[0].d;
            _hip = pts.Count > 1 ? pts[pts.Count - 1].d : default;
            _leftFootDev = default;
        }

        if (_hip.isValid && hipsBone != null &&
            _hip.TryGetFeatureValue(CommonUsages.devicePosition, out Vector3 hp))
        {
            _hipTrackerOffset = hipsBone.position - hp;
            _legLength = Mathf.Clamp(Mathf.Abs(hp.y - pts[0].p.y), 0.5f, 1.2f);
        }
        Debug.Log($"[FBT] 校准完成：hip={_hip.isValid} 左脚={_leftFootDev.isValid} 右脚={_rightFootDev.isValid} 腿长={_legLength:F2}m");
    }

    void LateUpdate()
    {
        if (!_vrActive || !autoEnable) return;
        if (hipsBone == null || headBone == null || lHandBone == null || rHandBone == null)
            CacheBones();
        if (hipsBone == null) return;

        // —— 顺序很重要：先髋/腿（移动整个身体），再手臂，最后头（头最后写，盖过髋部带动）——
        if (_calibrated && _hip.isValid &&
            _hip.TryGetFeatureValue(CommonUsages.devicePosition, out Vector3 hipPos))
        {
            hipsBone.position = hipPos + _hipTrackerOffset;
        }
        if (_calibrated)
        {
            if (_leftFootDev.isValid && _leftFootDev.TryGetFeatureValue(CommonUsages.devicePosition, out Vector3 lf))
                SolveTwoBone(_lUpperLeg, _lLowerLeg, lFootBone, lf, kneeHint);
            if (_rightFootDev.isValid && _rightFootDev.TryGetFeatureValue(CommonUsages.devicePosition, out Vector3 rf))
                SolveTwoBone(_rUpperLeg, _rLowerLeg, rFootBone, rf, kneeHint);
        }

        // 双手 → 手臂 IK
        var lh = InputDevices.GetDeviceAtXRNode(XRNode.LeftHand);
        if (lh.isValid && lh.TryGetFeatureValue(CommonUsages.devicePosition, out Vector3 lp))
        {
            SolveTwoBone(_lUpperArm, _lLowerArm, lHandBone, lp, elbowHint);
            if (lh.TryGetFeatureValue(CommonUsages.deviceRotation, out Quaternion lr))
                lHandBone.rotation = lr;
        }
        var rh = InputDevices.GetDeviceAtXRNode(XRNode.RightHand);
        if (rh.isValid && rh.TryGetFeatureValue(CommonUsages.devicePosition, out Vector3 rp))
        {
            SolveTwoBone(_rUpperArm, _rLowerArm, rHandBone, rp, elbowHint);
            if (rh.TryGetFeatureValue(CommonUsages.deviceRotation, out Quaternion rr))
                rHandBone.rotation = rr;
        }

        // 头最后写：贴到相机（相机已由 NetworkPlayerSync 按头显位姿驱动）
        var cam = Camera.main;
        if (cam != null && headBone != null)
        {
            headBone.position = cam.transform.position;
            headBone.rotation = cam.transform.rotation;
        }
    }

    /// <summary>通用双骨 IK（rig 无关）：upper 转朝目标方向，lower 绕 hint 平面折弯让 end 够到目标。</summary>
    void SolveTwoBone(Transform upper, Transform lower, Transform end, Vector3 target, Vector3 hint)
    {
        if (upper == null || lower == null || end == null) return;
        Vector3 A = upper.position, B = lower.position, C = end.position;
        float l1 = (B - A).magnitude;
        float l2 = (C - B).magnitude;
        if (l1 < 0.001f || l2 < 0.001f) return;

        Vector3 dir = target - A;
        float dist = Mathf.Clamp(dir.magnitude, 0.01f, l1 + l2 - 0.02f);
        dir = dir.normalized;

        // 1) upper 指向目标方向（肩→肘 对齐 dir，不依赖骨骼本地轴）
        Vector3 curUp = (B - A).normalized;
        upper.rotation = Quaternion.FromToRotation(curUp, dir) * upper.rotation;

        // 2) lower 绕「hint × dir」平面折弯，让 end 对准 target（肘/膝朝 hint 一侧）
        Vector3 curBC = (C - B).normalized;
        Vector3 wantBC = (target - B).normalized;
        if (curBC.sqrMagnitude < 0.0001f || wantBC.sqrMagnitude < 0.0001f) return;
        Vector3 axis = Vector3.Cross(hint, dir);
        if (axis.sqrMagnitude < 0.001f) axis = Vector3.Cross(upper.up, dir);
        if (axis.sqrMagnitude < 0.001f) axis = upper.right;
        axis.Normalize();
        float ang = Vector3.SignedAngle(curBC, wantBC, axis);
        lower.rotation = Quaternion.AngleAxis(ang, axis) * lower.rotation;
    }
}
