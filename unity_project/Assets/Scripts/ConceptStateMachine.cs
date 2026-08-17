// ConceptStateMachine.cs
// 接 Python 推来的「多念竞争」概念与动作关键词，驱动 Animator：
//   - 主念概念 name -> 触发 Animator Trigger（如 "探索""敌意""好奇"）；
//   - action 关键词 -> 触发对应动作 Trigger（如 "挥手""摇头""点头"）；
//   - 语音播放时：用 AudioSource 的频谱能量驱动身体起伏（呼吸/说话律动）。
// 另含 ProximitySensor 逻辑：玩家进入 3 米 -> 发 stimuli 给 Python，并转头 LookAt 玩家。
//
// 说明：Animator 里需自建对应 Trigger 参数（探索/敌意/好奇/挥手/摇头/点头 等）；
//       没有对应 Trigger 时静默忽略，不影响其他功能。

using System;
using System.Collections;
using UnityEngine;
using UnityEngine.AI;

[RequireComponent(typeof(Animator))]
[RequireComponent(typeof(AudioSource))]
public class ConceptStateMachine : MonoBehaviour
{
    private Animator _anim;
    private AudioSource _audio;

    [Header("接近感知")]
    public Transform player;                 // 拖入玩家物体（如 Main Camera 或 Player 胶囊）
    public float triggerRadius = 3f;         // 3 米内触发
    public float lookSpeed = 3f;
    private bool _playerNear;
    private NpcBridgeClient _bridge;

    [Header("说话律动")]
    public float bodyBobScale = 0.05f;       // 身体起伏幅度
    private float _bobPhase;

    [Header("站姿")]
    [Tooltip("手臂自然垂放角度。若手臂交叉/朝上，先切 mirrorArmZ；还不对再调这个角")]
    public float restArmAngle = 75f;
    [Tooltip("手臂绕Z轴方向：true=左臂+右臂-；false=反过来。双臂交叉/方向反了就先切它")]
    public bool mirrorArmZ = true;
    [Tooltip("待机头部抬升角(度)：补偿模型自带的前倾/低头，正值=抬头")]
    public float headLiftAngle = 8f;

    [Header("自检(上线前关掉)")]
    public bool autoTestOnStart = false;     // 启动后自动播一次 ACT_WAVE，绕过 Python 验证动画链路
    public float autoTestDelay = 2f;

    // 程序化挥手需要的骨骼（Humanoid 标准骨骼）——直接写 Transform 保证在无控制器的 VRM 上也必定生效
    private Transform _leftUpperArm;
    private Transform _leftLowerArm;
    private Transform _rightUpperArm;
    private Transform _rightLowerArm;
    private Transform _spine;
    private Transform _chest;
    private Transform _neck;
    private Transform _head;
    private bool _poseDebugLogged;

    void Awake()
    {
        _anim = GetComponentInChildren<Animator>();
        _audio = GetComponent<AudioSource>();
        _bridge = GetComponent<NpcBridgeClient>();

        // 缓存骨骼用于程序化姿态
        if (_anim != null && _anim.avatar != null && _anim.avatar.isHuman)
        {
            _leftUpperArm = _anim.GetBoneTransform(HumanBodyBones.LeftUpperArm);
            _leftLowerArm = _anim.GetBoneTransform(HumanBodyBones.LeftLowerArm);
            _rightUpperArm = _anim.GetBoneTransform(HumanBodyBones.RightUpperArm);
            _rightLowerArm = _anim.GetBoneTransform(HumanBodyBones.RightLowerArm);
            _spine = _anim.GetBoneTransform(HumanBodyBones.Spine);
            _chest = _anim.GetBoneTransform(HumanBodyBones.Chest);
            _neck = _anim.GetBoneTransform(HumanBodyBones.Neck);
            _head = _anim.GetBoneTransform(HumanBodyBones.Head);
            Debug.Log($"[ConceptSM] 手臂骨骼: L.upper={_leftUpperArm?.name} L.lower={_leftLowerArm?.name} " +
                      $"R.upper={_rightUpperArm?.name} R.lower={_rightLowerArm?.name}");
        }
        else
        {
            Debug.LogWarning("[ConceptSM] 模型不是 Humanoid 或没有 Animator，程序化挥手将仅移动整体");
        }
    }

    void Start()
    {
        // 启动常驻“生命感”叠加层：呼吸 + 待机随机转头/眨眼
        if (_anim != null && _anim.avatar != null && _anim.avatar.isHuman)
        {
            _breathCoroutine = StartCoroutine(ProceduralBreath());
            _idleLookCoroutine = StartCoroutine(ProceduralIdleLook());
        }
        if (autoTestOnStart && _anim != null)
            StartCoroutine(AutoTest());

        // 记录身体 bob 基准位置（说话/挥手律动都相对它偏移，避免位移累积漂移/瞬跳）
        _bobBase = transform.position;

        Debug.Log($"[ConceptSM] 就绪 — TriggerAction(speed/amplitude/lean/trait) 全部支持, " +
                   $"restlessness={_restlessness:F2} breath={_breathRate:F2} " +
                   $"humanoid={(_anim!=null&&_anim.avatar!=null&&_anim.avatar.isHuman)} autoTest={autoTestOnStart}");
    }

    // 外部（Python/clayer）设置躁动度：无聊→低，等待/期待→高
    public void SetRestlessness(float v)
    {
        _restlessness = Mathf.Clamp01(v);
        _breathRate = Mathf.Lerp(0.6f, 1.6f, _restlessness); // 越躁动呼吸越快
    }

    // 常驻呼吸：仅做胸腔/脊柱的「扩张收缩」起伏，营造活人呼吸感，
    // 但【绝对不能持续前倾躯干】，否则待机看起来像一直在弯腰。
    // 做法：用 Chest/Spine 的极小幅度「左右轻微扭动 + 极轻缩放感」，
    // 呼吸相位用 Quaternion.Slerp 实时回正，保证平均姿态=直立。
    private IEnumerator ProceduralBreath()
    {
        float t = 0f;
        while (true)
        {
            t += Time.deltaTime;
            float breath = Mathf.Sin(t * 1.2f * _breathRate); // -1..1，正负对称→平均直立
            // 仅 ±2.2 度的极轻胸腔起伏（绕 Z 轴左右微摆），不引入任何前倾(X轴)补偿。
            // 关键点：breath 是正弦（有正有负），平均 0 → 平均姿态=Identity（直立）。
            _breathSpineRot = Quaternion.Euler(breath * 2.2f, 0f, 0f);   // 前后轻晃=呼吸感
            _breathChestRot = Quaternion.Euler(breath * 1.6f, 0f, 0f);
            yield return null;
        }
    }

    // 待机微表情：偶尔转头看看玩家/四周，避免“死盯前方”的僵硬
    // 用 Neck 的 Y 轴（点头只动 Head 的 X 轴，互不干扰），叠加层权重低
    private IEnumerator ProceduralIdleLook()
    {
        float t = 0f;
        float             nextTurnIn = UnityEngine.Random.Range(2f, 5f);
        float turnDir = 0f;
        float turnTarget = 0f;
        while (true)
        {
            t += Time.deltaTime;
            // 越躁动转头越频繁
            float interval = Mathf.Lerp(6f, 2f, _restlessness);
            if (t >= nextTurnIn)
            {
                turnTarget = UnityEngine.Random.Range(-25f, 25f);
                nextTurnIn = t + UnityEngine.Random.Range(interval * 0.6f, interval * 1.4f);
            }
            turnDir = Mathf.Lerp(turnDir, turnTarget, Time.deltaTime * 1.5f);
            // 缓慢回正
            if (t >= nextTurnIn - 0.5f) turnTarget *= 0.95f;
            _idleNeckRot = Quaternion.Euler(0f, turnDir, 0f);
            yield return null;
        }
    }

    private IEnumerator AutoTest()
    {
        yield return new WaitForSeconds(autoTestDelay);
        Debug.Log("[ConceptSM] [自检] 自动触发 ACT_WAVE");
        TriggerAction("ACT_WAVE", 2f);
    }

    // ---- 由 NpcBridgeClient 调用 ----
    public void TriggerConcept(string name, float weight)
    {
        if (_anim == null || string.IsNullOrEmpty(name)) return;
        // 无 AnimatorController 时 SetTrigger/SetFloat 会刷黄色警告且无效，直接跳过
        if (_anim.runtimeAnimatorController == null) return;
        string trigger = Sanitize(name);
        // 概念名可能带空格/中文，Unity Trigger 名用 ASCII 更稳定：这里直接尝试
        // （第二关排查：打印实际触发的 Trigger 名，方便和 Animator 里建的逐一比对）
        Debug.Log($"[ConceptSM] TriggerConcept: {name} -> trigger='{trigger}' (weight={weight})");
        try { _anim.SetTrigger(trigger); }
        catch (System.Exception e) { Debug.LogWarning($"[ConceptSM] 概念 Trigger 失败: {trigger} -> {e.Message}"); }
        // 概念强度也可驱动一个 Float 参数（若 Animator 有）
        try { _anim.SetFloat("ConceptWeight", weight); } catch { }
    }

    private Coroutine _actionCoroutine;
    private Coroutine _proceduralCoroutine;   // 用于 LOOKAROUND / FOLLOW 等“占位”身姿
    private Coroutine _waveCoroutine;         // 挥手用独立协程，避免与 LOOKAROUND 互斥
    private Coroutine _nodCoroutine;          // 点头用独立协程
    private Coroutine _breathCoroutine;       // 常驻呼吸（叠加层，不影响动作）
    private Coroutine _idleLookCoroutine;     // 待机随机转头/眨眼（叠加层）

    // 叠加层（呼吸/微表情）目标旋转——始终生效，权重低，营造“活人感”
    private Quaternion _breathSpineRot = Quaternion.identity;  // Spine 起伏
    private Quaternion _breathChestRot = Quaternion.identity;
    private Quaternion _idleNeckRot = Quaternion.identity;     // Neck 随机转头
    // 待机“挺直”修正：本项目用的 Idle 剪辑是 Starter Assets 第三人称男模的
    // Stand--Idle，它本身带明显含胸 + 脊柱前倾（看起来像一直在弯腰）。
    // 这里每帧对 Spine/Chest 施加固定后展(-X)，把含胸姿态拉回到接近直立。
    // 幅度约 -22°(Spine) / -18°(Chest)，足以抵消该 Idle 的前倾，又不至于后仰。
    private Quaternion _standTallSpine = Quaternion.Euler(-22f, 0f, 0f);
    private Quaternion _standTallChest = Quaternion.Euler(-18f, 0f, 0f);
    private float _breathRate = 0.9f;      // 呼吸频率（空闲低、等待高）
    private float _restlessness = 0.2f;   // 躁动度(0~1)：越高呼吸越快、转头越频

    // 程序化挥手的目标旋转（由协程计算，在 OnAnimatorIK 中应用）
    private Quaternion _waveUpperArmRot = Quaternion.identity;   // 目标
    private Quaternion _waveLowerArmRot = Quaternion.identity;   // 目标
    private Quaternion _waveUpperArmCur = Quaternion.identity;   // 当前（阻尼跟随目标）
    private Quaternion _waveLowerArmCur = Quaternion.identity;
    private bool _waveActive;

    // 程序化点头的目标旋转（在 OnAnimatorIK 中应用）
    private Quaternion _nodHeadRot = Quaternion.identity;
    private Quaternion _nodChestRot = Quaternion.identity;
    private Quaternion _nodHeadCur = Quaternion.identity;
    private Quaternion _nodChestCur = Quaternion.identity;
    private bool _nodActive;

    [Header("动作平滑")]
    public float actionSmooth = 12f;   // 阻尼系数：越大越跟手，越小越柔（解决“生硬/瞬移”）

    private Vector3 _bobBase = Vector3.zero;   // 身体 bob 基准位置（避免位移漂移/跳变）

    // 动作收尾计时器：动作协程结束后，仍继续在 IK 写 cur 并把 cur 阻尼回零位，
    // 时长由该计时器决定。绝不能用 IsNearIdentity 在动作中判断是否写（正弦摆动经过
    // 0 点时会误判为“已归位”而瞬停→手臂掉回 Idle→抖动）。
    private float _waveTail = 0f;
    private float _nodTail = 0f;

    private string _curTrait = "";   // 当前动作的性格标签（Python 端 emotion.motion_params 下发）
    private float _curLean = 0f;     // 当前动作的身体倾向（>0 前倾/亲近，<0 后缩/矜持）

    private bool HasAnimatorParameter(string name)
    {
        if (_anim == null || string.IsNullOrEmpty(name)) return false;
        foreach (var param in _anim.parameters)
        {
            if (param.name == name) return true;
        }
        return false;
    }

    public void TriggerAction(string action, float duration = 0f, float speed = 1f, float amplitude = 1f, string trait = "", float lean = 0f)
    {
        if (_anim == null || string.IsNullOrEmpty(action)) return;

        // 记录本次动作的性格/倾向（情感×性格融合，由 Python 端 emotion.motion_params 算出），
        // 供程序化动作(挥手/点头/环顾)塑形：lean>0 更前倾(黏人)，<0 更后缩(矜持/傲娇)。
        _curTrait = trait ?? "";
        _curLean = lean;

        string trigger = action;
        if (action.StartsWith("[ACT_") && action.EndsWith("]"))
            trigger = action.Substring(1, action.Length - 2); // -> ACT_IDLE
        // Python 端用简单名 wave，Animator 里用的 trigger 名是 ACT_WAVE
        if (!trigger.StartsWith("ACT_", StringComparison.OrdinalIgnoreCase))
            trigger = "ACT_" + trigger.ToUpperInvariant();

        Debug.Log($"[ConceptSM] Triggering: {trigger} (duration={duration})");

        // 没有现成动画文件的情绪/反应动作，先用程序化身姿让反应可见。
        // 注意：挥手用独立协程 _waveCoroutine，故意不与 LOOKAROUND 共享
        // _proceduralCoroutine，否则高频自发动作会长期占用该变量，导致用户
        // 输入触发的 ACT_WAVE 因“_proceduralCoroutine != null”被整段吞掉
        // （现象：开场自检能挥手，但聊天时永远不挥手）。
        if (trigger == "ACT_WAVE")
        {
            // 允许打断上一次挥手，重新计时
            if (_waveCoroutine != null) StopCoroutine(_waveCoroutine);
            _waveCoroutine = StartCoroutine(ProceduralWave(duration > 0f ? duration : 1.5f, speed, amplitude));
        }
        else if (trigger == "ACT_NOD")
        {
            // 普通聊天的轻量反应：轻微点头/上身前倾，自然不突兀。
            // 用独立协程，可与挥手/环顾并存，不被互斥。
            if (_nodCoroutine != null) StopCoroutine(_nodCoroutine);
            _nodCoroutine = StartCoroutine(ProceduralNod(duration > 0f ? duration : 1.0f, speed, amplitude));
        }
        else if (trigger == "ACT_LOOKAROUND" && _proceduralCoroutine == null)
            _proceduralCoroutine = StartCoroutine(ProceduralLookAround(duration > 0f ? duration : 2f));
        else if (trigger == "ACT_TURN" && _proceduralCoroutine == null)
            _proceduralCoroutine = StartCoroutine(ProceduralTurn(duration > 0f ? duration : 1.2f));
        else if (trigger == "ACT_STAND")
        {
            // 立正：停止所有程序化身姿，姿态回归直立（Idle）。
            ResetPose();
        }
        else if ((trigger == "ACT_FOLLOW" || trigger == "ACT_POINT") && player != null)
            FacePlayerOnce(duration > 0f ? duration : 1f);

        try
        {
            // 把情绪驱动的 speed/amplitude 写入 Animator 参数（建议第一步：
            // 同一动作的速度/幅度变化）。即使挥手是程序化骨骼驱动，也同步记录，
            // 便于将来接 Blend Tree 或调试观察。
            if (HasAnimatorParameter("WaveSpeed"))
                _anim.SetFloat("WaveSpeed", speed);
            if (HasAnimatorParameter("WaveAmp"))
                _anim.SetFloat("WaveAmp", amplitude);

            // 仅用 Trigger 触发：AnyState -> 对应状态，播放完由 HoldAction 回到 Idle。
            // 不要再调 _anim.Play(trigger,...) —— trigger 名(如 ACT_WAVE)与状态机
            // 状态名(如 Wave)不同，直接 Play 状态名会找不到/覆盖过渡导致不动。
            // 注意：部分动作(如 ACT_NOD)是程序化身姿，Animator 里没对应参数，
            // 直接 SetTrigger 会报 "Parameter does not exist"。先检查再调用。
            if (HasAnimatorParameter(trigger))
                _anim.SetTrigger(trigger);
            else
                Debug.Log($"[ConceptSM] Animator 无参数 {trigger}，使用程序化身姿");
        }
        catch (System.Exception e)
        {
            Debug.LogWarning($"[ConceptSM] 动作 Trigger 失败: {trigger} -> {e.Message}");
            return;
        }

        if (duration > 0f)
        {
            if (_actionCoroutine != null) StopCoroutine(_actionCoroutine);
            _actionCoroutine = StartCoroutine(HoldAction(trigger, duration));
        }
    }

    private IEnumerator HoldAction(string trigger, float duration)
    {
        yield return new WaitForSeconds(duration);
        if (_anim != null && _anim.runtimeAnimatorController != null)
        {
            try { _anim.CrossFade("Idle", 0.25f, 0); } catch { }
            if (HasAnimatorParameter(trigger))
                _anim.ResetTrigger(trigger);
        }
    }

    public void OnSpeechStart() { if (_anim != null && _anim.runtimeAnimatorController != null) { try { _anim.SetBool("IsTalking", true); } catch { } } }
    public void OnSpeechStop() { if (_anim != null && _anim.runtimeAnimatorController != null) { try { _anim.SetBool("IsTalking", false); } catch { } } }

    public void OnAudioPlay(AudioSource src) { _audio = src; }

    // ---- 每帧：接近感知 + 说话律动 ----
    void Update()
    {
        // 接近感知
        if (player != null)
        {
            float dist = Vector3.Distance(transform.position, player.position);
            bool near = dist <= triggerRadius;
            if (near && !_playerNear)
            {
                _playerNear = true;
                // 发 stimuli 给 Python（"玩家接近"+"社交"）
                _bridge?.SendStimuli(new System.Collections.Generic.List<string> { "玩家接近", "社交" }, 0.9f);
                // 面向玩家
                FacePlayer();
            }
            else if (!near && _playerNear)
            {
                _playerNear = false;
            }
            if (_playerNear && !_lookLocked) FacePlayer();
        }

        // 说话律动：用音频频谱能量驱动身体起伏
        if (_audio != null && _audio.isPlaying)
        {
            float energy = GetAudioEnergy(_audio);
            _bobPhase += Time.deltaTime * (4f + energy * 8f); // 语速越快起伏越快
            float bob = Mathf.Sin(_bobPhase) * bodyBobScale * (0.3f + energy);
            // 关键修复：绝对赋值（相对基准 _bobBase 偏移），绝不 += 累积，
            // 否则每帧往上累加、说话结束不归零 → 角色被永久抬高 / 挥手时跳变。
            transform.position = _bobBase + Vector3.up * bob;
            // 也可驱动 Animator Float 表现呼吸/说话强度
            try { _anim.SetFloat("VoiceEnergy", energy); } catch { }
        }
        else
        {
            // 没在说话时，基准跟着角色当前位置走（兼容行走/移动），
            // 但保留挥手协程临时写入的位置（挥手时 _bobBase 已被协程设为正确基准）。
            if (!_waveActive && _waveTail <= 0f)
                _bobBase = transform.position;
        }
    }

    private bool _lookLocked; // 程序化身姿执行期间暂时接管朝向

    void FacePlayer()
    {
        Vector3 dir = (player.position - transform.position);
        dir.y = 0;
        if (dir == Vector3.zero) return;
        Quaternion target = Quaternion.LookRotation(dir);
        transform.rotation = Quaternion.Slerp(transform.rotation, target, lookSpeed * Time.deltaTime);
    }

    private IEnumerator ProceduralLookAround(float duration)
    {
        _lookLocked = true;
        Quaternion origin = transform.rotation;
        float t = 0f;
        while (t < duration)
        {
            t += Time.deltaTime;
            float p = Mathf.Sin(t * 3f); // 左右看
            float angle = p * 40f;       // ±40 度（环顾四周：小幅左右看，不是整圈转身）
            transform.rotation = Quaternion.Euler(origin.eulerAngles.x, origin.eulerAngles.y + angle, origin.eulerAngles.z);
            yield return null;
        }
        transform.rotation = origin;
        _lookLocked = false;
        _proceduralCoroutine = null;
    }

    // 转身：完整地转 180° 面向身后（"转身/转过去"语义），不是左右摇摆。
    // 用 Slerp 平滑转到 origin*180°，结束保持朝后（除非玩家接近/说话会重新 FacePlayer）。
    private IEnumerator ProceduralTurn(float duration)
    {
        _lookLocked = true;
        Quaternion from = transform.rotation;
        Quaternion to = from * Quaternion.Euler(0f, 180f, 0f);
        float t = 0f;
        while (t < duration)
        {
            t += Time.deltaTime;
            float k = Mathf.SmoothStep(0f, 1f, Mathf.Clamp01(t / Mathf.Min(duration, 0.6f)));
            transform.rotation = Quaternion.Slerp(from, to, k);
            yield return null;
        }
        transform.rotation = to;
        _lookLocked = false;
        _proceduralCoroutine = null;
    }

    // 立正：撤销所有程序化身姿（挥手/点头/呼吸覆盖层归零），姿态回到 Idle 直立。
    public void ResetPose()
    {
        // 平滑立正：只停协程 + 把目标设回零位 + 关闭写入标志，
        // 但【不立即清零 cur】——OnAnimatorIK 会持续写 cur 直到阻尼回零，
        // 实现从当前姿态平滑过渡回直立（而不是瞬间 snap）。
        StopAllProcedural();
        _waveUpperArmRot = Quaternion.identity;
        _waveLowerArmRot = Quaternion.identity;
        _waveActive = false;
        _waveTail = 0.5f;   // 平滑收尾，避免立正时手臂瞬间 snap
        _nodHeadRot = Quaternion.identity;
        _nodChestRot = Quaternion.identity;
        _nodActive = false;
        _nodTail = 0.5f;
        transform.position = _bobBase;   // 立正：身体位置也回到基准，消除残留偏移
        // 注意：呼吸/微表情叠加层(挺直修正)保留，立正后仍是自然直立待机，
        // 不要清零 breath/idle，否则反而失去挺直修正。
    }

    private void StopAllProcedural()
    {
        if (_waveCoroutine != null) { StopCoroutine(_waveCoroutine); _waveCoroutine = null; }
        if (_nodCoroutine != null) { StopCoroutine(_nodCoroutine); _nodCoroutine = null; }
        if (_proceduralCoroutine != null) { StopCoroutine(_proceduralCoroutine); _proceduralCoroutine = null; }
    }

    // 自然挥手（参考真人挥手 + 迪士尼动画12原则：跟随/重叠动作、渐入渐出）：
    //  真人挥手 = 手臂以【肩为支点的钟摆】，手划出弧线；且手腕/手【滞后】于小臂
    //  （跟随动作 follow-through：手臂摆到一侧时，手因惯性晚一点才追上 → 末端甩动感）。
    //  Unity Humanoid 左臂本地轴：X=前，Y=上，Z=指向身体右侧(即朝内)。
    //  - 大臂外展(绕Z负) + 前举(绕X正)：把整条手臂抬成侧举预备姿态（钟摆支点）
    //  - 肘屈(绕X正 ~90°)：小臂竖起
    //  - 挥动：绕 Z 轴做钟摆摆动，但【手腕比小臂滞后一个相位】→ 手末端甩动(重叠动作)
    //  - 渐入渐出包络：摆幅在中间最大、两端小(ease)，不是恒定幅度（更自然）
    //  - 身体重叠动作：随挥动节奏做极轻的整体上下 bob（肩/躯干跟着动）
    //  - 平滑：目标经阻尼跟随，结束自然落回 Idle
    private IEnumerator ProceduralWave(float duration, float speed = 1f, float amplitude = 1f)
    {
        if (_anim == null || _anim.avatar == null || !_anim.avatar.isHuman)
        {
            _waveCoroutine = null;
            yield break;
        }

        speed = Mathf.Clamp(speed, 0.3f, 1.4f);
        amplitude = Mathf.Clamp(amplitude, 0.6f, 1.0f);

        _waveActive = true;
        _bobBase = transform.position;   // 记录基准，bob 相对它偏移，避免累积漂移
        float t = 0f;
        float freq = 6.5f * speed;               // 钟摆摆动频率（情绪高略快）
        float baseShoulder = 62f * amplitude;    // 大臂外展角（侧举预备）
        float baseForward = 20f * amplitude;     // 略向前举
        float baseElbow = 88f * amplitude;       // 肘屈（小臂竖起）
        float oscAmp = 26f * amplitude;          // 钟摆摆幅
        float handLag = 0.32f;                   // 手腕滞后小臂的相位（秒）→ 末端甩动感
        float fadeIn = 0.28f;                    // 抬臂缓入
        float fadeOut = 0.40f;                   // 落臂缓出（更柔）
        float wavePhase = UnityEngine.Random.Range(0f, Mathf.PI * 2f); // 随机相位，不机械
        while (t < duration)
        {
            t += Time.deltaTime;
            // 抬/落臂包络（线性缓入缓出）
            float lift = Mathf.Min(
                Mathf.Clamp01(t / fadeIn),
                1f - Mathf.Clamp01((t - (duration - fadeOut)) / fadeOut));
            // 摆动幅度包络：中间最欢快、首尾轻柔（渐入渐出 ease）
            float swingEnv = Mathf.Sin(Mathf.PI * Mathf.Clamp01(t / duration)); // 0→1→0
            swingEnv = swingEnv * swingEnv * (3f - 2f * swingEnv);              // smoothstep

            // 钟摆主摆角（驱动整条手臂绕 Z 摆）
            float pend = Mathf.Sin(t * freq + wavePhase) * oscAmp * lift * swingEnv;
            // 大臂：外展(绕Z负) + 前举(绕X正) + 跟随钟摆的轻微摆动
            float shoulderSign = mirrorArmZ ? 1f : -1f;   // 与垂臂方向保持一致
            float upperZ = shoulderSign * baseShoulder * lift + pend * 0.35f;
            float upperX = baseForward * lift;
            // 小臂：肘屈(绕X正) + 钟摆摆动（比大臂略大，手臂像整体在挥）
            float lowerX = baseElbow * lift;
            float lowerZ = pend;
            // 手腕/手【滞后】于小臂一个相位 → 末端甩动(重叠动作/follow-through)
            float handPend = Mathf.Sin((t - handLag) * freq + wavePhase) * oscAmp * lift * swingEnv;
            // 把滞后量折算成手腕额外绕 Z 的差值（手比小臂晚到 → 形成甩动弧线）
            float wristExtra = (handPend - pend) * 0.5f;
            lowerZ += wristExtra;

            // _curLean（性格倾向）：>0 黏人→挥手时身体更往前送，<0 傲娇/敏感→略后收
            // 折算成上臂前举(X)的小幅偏移，让性格在肢体上可见（不影响动作本质）。
            float leanX = _curLean * 18f * lift;
            _waveUpperArmRot = Quaternion.Euler(upperX + leanX, 0f, upperZ);
            _waveLowerArmRot = Quaternion.Euler(lowerX, 0f, lowerZ);

            // 身体重叠动作：随挥动节奏极轻上下 bob（肩/躯干跟着动，不是死站）
            // 用基准位置偏移，不累积，避免结束瞬跳/漂移
            float bob = Mathf.Sin(t * freq + wavePhase) * 0.012f * lift * swingEnv;
            transform.position = _bobBase + Vector3.up * bob;

            yield return null;
        }

        // 目标归零（手臂垂下），由 OnAnimatorIK 的阻尼平滑拉回 Idle
        _waveUpperArmRot = Quaternion.identity;
        _waveLowerArmRot = Quaternion.identity;
        transform.position = _bobBase;   // 恢复基准，消除任何残留偏移
        _waveActive = false;
        _waveTail = 0.5f;   // 开启收尾：继续写 IK 把 cur 阻尼回零，避免瞬切
        _waveCoroutine = null;
    }

    // 轻量点头：头部小幅上下点动 + 上身高频微前倾，表达“在听/认同/回应”
    // 目标值经阻尼跟随，结束自然回落，去生硬
    private IEnumerator ProceduralNod(float duration, float speed = 1f, float amplitude = 1f)
    {
        if (_anim == null || _anim.avatar == null || !_anim.avatar.isHuman)
        {
            _nodCoroutine = null;
            yield break;
        }

        _nodActive = true;
        float t = 0f;
        float freq = 7f * Mathf.Clamp(speed, 0.5f, 1.4f);      // 情绪高时点头更频
        float maxHead = 22f * Mathf.Clamp(amplitude, 0.55f, 1.0f);   // 点头幅度加大，反应更明显
        float maxChest = 4f * Mathf.Clamp(amplitude, 0.55f, 1.0f);
        float fadeIn = 0.15f;
        float fadeOut = 0.2f;
        while (t < duration)
        {
            t += Time.deltaTime;
            float env = Mathf.Min(
                Mathf.Clamp01(t / fadeIn),
                1f - Mathf.Clamp01((t - (duration - fadeOut)) / fadeOut));
            float nod = Mathf.Sin(t * freq) * 0.5f + 0.5f; // 0..1
            float headPitch = Mathf.Lerp(0f, maxHead, nod) * env;
            float chestPitch = Mathf.Lerp(0f, maxChest, nod) * env;

            // 性格倾向 _curLean：点头时黏人→上身略前倾点得更深，傲娇/敏感→略后收
            float leanChest = chestPitch + _curLean * 10f * env;
            _nodHeadRot = Quaternion.Euler(headPitch, 0f, 0f);
            _nodChestRot = Quaternion.Euler(leanChest, 0f, 0f);

            yield return null;
        }

        _nodHeadRot = Quaternion.identity;
        _nodChestRot = Quaternion.identity;
        _nodActive = false;
        _nodTail = 0.5f;   // 开启收尾：继续写 IK 把 cur 阻尼回零，避免瞬切
        _nodCoroutine = null;
    }

    // 关键：SetBoneLocalRotation 原来只在 OnAnimatorIK 中调用——但 OnAnimatorIK
    // 只有 Animator 正在播放 AnimatorController 时才会被 Unity 回调。重建后的
    // VRM 实例 Animator 没有挂控制器 → OnAnimatorIK 从不触发 → 所有程序化身姿
    // （挥手/点头/呼吸）算完却从未写到骨头上（现象：有气泡回复、无动作）。
    // 修复：应用逻辑抽成 ApplyProceduralPose()，双入口兜底：
    //   · 有控制器时走 OnAnimatorIK（官方推荐时机，与动画混合最稳）；
    //   · 无控制器时走 LateUpdate（每帧必调，程序化身姿在无控制器模型上也能动）。
    void OnAnimatorIK(int layerIndex)
    {
        ApplyProceduralPose();
    }

    void LateUpdate()
    {
        // 无论有没有 AnimatorController 都执行：
        //  · 无控制器：OnAnimatorIK 永不触发，必须靠 LateUpdate（旧路径只覆盖这种情况）；
        //  · 有控制器但 IK Pass 没勾：OnAnimatorIK 也不触发，同样只能靠 LateUpdate；
        //  · 有控制器且 IK Pass 勾选：两个入口都跑，写的是同一组值，幂等无害。
        ApplyProceduralPose();
    }

    private void ApplyProceduralPose()
    {
        if (_anim == null || _anim.avatar == null || !_anim.avatar.isHuman)
            return;

        if (!_poseDebugLogged)
        {
            _poseDebugLogged = true;
            Debug.Log($"[ConceptSM] 姿态直写已启用: spine={_spine?.name} chest={_chest?.name} " +
                      $"neck={_neck?.name} head={_head?.name} restArmAngle={restArmAngle} " +
                      $"controller={( _anim.runtimeAnimatorController != null)}");
        }

        bool poseBusy = _waveActive || _nodActive;
        // 挺直修正只针对"有控制器且其 Idle 含胸"的老模型（Starter Assets）；
        // 无控制器的 VRM 没有前倾要纠正，套用会变成明显后仰——这里只在有控制器时施加。
        bool hasController = _anim.runtimeAnimatorController != null;
        Quaternion spineFix = (poseBusy || !hasController) ? Quaternion.identity : _standTallSpine;
        Quaternion chestFix = (poseBusy || !hasController) ? Quaternion.identity : _standTallChest;

        // —— 双通道写入：Animator 通道 + 直接写骨骼 Transform ——
        // 无 AnimatorController 的 VRM 上 SetBoneLocalRotation 可能不生效（旧现象：
        // 姿态从未显示、只剩服饰 SpringBone 在动），直接写 Transform 保证必定生效。
        _anim.SetBoneLocalRotation(HumanBodyBones.Spine, spineFix * _breathSpineRot);
        _anim.SetBoneLocalRotation(HumanBodyBones.Chest, chestFix * _breathChestRot);
        _anim.SetBoneLocalRotation(HumanBodyBones.Neck, _idleNeckRot);
        if (_spine != null) _spine.localRotation = spineFix * _breathSpineRot;
        if (_chest != null) _chest.localRotation = chestFix * _breathChestRot;
        if (_neck != null) _neck.localRotation = _idleNeckRot;

        // 手臂自然垂放（rig 无关实现：用"肩→肘"的实际世界方向，不依赖骨骼本地轴假设）
        if (!_waveActive)
        {
            PointBoneDown(_leftUpperArm, _leftLowerArm);
            if (_leftLowerArm != null) _leftLowerArm.localRotation = Quaternion.identity;
        }
        PointBoneDown(_rightUpperArm, _rightLowerArm);
        if (_rightLowerArm != null) _rightLowerArm.localRotation = Quaternion.identity;

        // 阻尼跟随：当前值向目标值 Slerp，消除每帧硬跳 / 结束瞬移（解决“生硬”）
        float k = 1f - Mathf.Exp(-actionSmooth * Time.deltaTime);

        // 写入条件：动作进行中(_*Active) 或 收尾期(_*Tail>0)。
        bool waveWriting = _waveActive || _waveTail > 0f;
        if (waveWriting)
        {
            _waveUpperArmCur = Quaternion.Slerp(_waveUpperArmCur, _waveUpperArmRot, k);
            _waveLowerArmCur = Quaternion.Slerp(_waveLowerArmCur, _waveLowerArmRot, k);
            _anim.SetBoneLocalRotation(HumanBodyBones.LeftUpperArm, _waveUpperArmCur);
            _anim.SetBoneLocalRotation(HumanBodyBones.LeftLowerArm, _waveLowerArmCur);
            if (_leftUpperArm != null) _leftUpperArm.localRotation = _waveUpperArmCur;
            if (_leftLowerArm != null) _leftLowerArm.localRotation = _waveLowerArmCur;
            if (!_waveActive) _waveTail -= Time.deltaTime;
        }
        bool nodWriting = _nodActive || _nodTail > 0f;
        if (nodWriting)
        {
            _nodHeadCur = Quaternion.Slerp(_nodHeadCur, _nodHeadRot, k);
            _nodChestCur = Quaternion.Slerp(_nodChestCur, _nodChestRot, k);
            _anim.SetBoneLocalRotation(HumanBodyBones.Head, _nodHeadCur);
            _anim.SetBoneLocalRotation(HumanBodyBones.Chest, _nodChestCur);
            if (_head != null) _head.localRotation = _nodHeadCur;
            if (_chest != null) _chest.localRotation = _nodChestCur;
            if (!_nodActive) _nodTail -= Time.deltaTime;
        }
        // 待机抬头微调（补偿绑定位低头；负X=抬头）。点头时交给 nod 接管。
        if (!nodWriting && _head != null)
        {
            var lift = Quaternion.Euler(-headLiftAngle, 0f, 0f);
            _head.localRotation = lift;
            _anim.SetBoneLocalRotation(HumanBodyBones.Head, lift);
        }
    }

    /// <summary>把上臂转到竖直向下：用肩→肘的实际世界方向做 FromToRotation，与骨骼本地轴无关。</summary>
    private void PointBoneDown(Transform shoulder, Transform elbow)
    {
        if (shoulder == null || elbow == null) return;
        Vector3 dir = (elbow.position - shoulder.position).normalized;
        if (dir.sqrMagnitude < 0.0001f) return;
        shoulder.rotation = Quaternion.FromToRotation(dir, Vector3.down) * shoulder.rotation;
    }

    private IEnumerator FacePlayerOnce(float duration)
    {
        _lookLocked = true;
        float t = 0f;
        while (t < duration && player != null)
        {
            t += Time.deltaTime;
            Vector3 dir = (player.position - transform.position);
            dir.y = 0;
            if (dir != Vector3.zero)
            {
                Quaternion target = Quaternion.LookRotation(dir);
                transform.rotation = Quaternion.Slerp(transform.rotation, target, lookSpeed * Time.deltaTime);
            }
            yield return null;
        }
        _lookLocked = false;
    }

    // 取当前音频帧的平均能量（0~1），用于身体律动
    float GetAudioEnergy(AudioSource src)
    {
        float[] samples = new float[256];
        src.GetSpectrumData(samples, 0, FFTWindow.BlackmanHarris);
        float sum = 0;
        for (int i = 0; i < samples.Length; i++) sum += samples[i];
        return Mathf.Clamp01(sum / samples.Length * 10f);
    }

    // 把概念/动作名清洗成合法 Trigger 名（去空格、限 ASCII）
    static string Sanitize(string s)
    {
        if (string.IsNullOrEmpty(s)) return s;
        var sb = new System.Text.StringBuilder();
        foreach (char c in s)
        {
            if (char.IsLetterOrDigit(c)) sb.Append(char.ToLower(c));
        }
        return sb.ToString();
    }
}
