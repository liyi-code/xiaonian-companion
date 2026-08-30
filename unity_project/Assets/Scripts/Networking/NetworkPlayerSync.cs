// NetworkPlayerSync.cs —— 玩家化身同步（NetworkCharacterController 版）
// ============================================================================
// 挂在 NetworkPlayer 预制体上（NetworkSetup 生成：胶囊 + NCC + Rigidbody + 相机）。
// 为什么用 NCC：Shared 模式下 NetworkTransform 会把网络快照写回自己的根物体，
// 覆盖 Update 里的移动/转向（表现：只能俯仰、不能走不能转）。NCC（NetworkTRSP）
// 为玩家控制专门设计：FixedUpdateNetwork 里 Move() 移动，旋转直接写 transform，
// 权威端不受插值回写影响，远端照常同步。
// 操作：WASD 移动（Update 采集输入 → FixedUpdateNetwork 执行），鼠标左右转向、
// 上下俯仰（相机子物体，不联网）。
// ============================================================================
using Fusion;
using UnityEngine;
using UnityEngine.XR;

[DefaultExecutionOrder(300)]   // 骨骼同步读/写跑在 CSM / VrFullBodyTracking 之后，避免同帧打架
[RequireComponent(typeof(NetworkObject))]
[RequireComponent(typeof(NetworkCharacterController))]
public class NetworkPlayerSync : NetworkBehaviour
{
    // —— 化身骨骼联网同步（顺序固定）——
    // 0 Hips 1 Spine 2 Chest 3 Neck 4 Head
    // 5 左上臂 6 左小臂 7 左手 8 右上臂 9 右小臂 10 右手
    // 11 左大腿 12 左小腿 13 左脚 14 右大腿 15 右小腿 16 右脚
    public const int BONE_COUNT = 17;
    [Networked, Capacity(BONE_COUNT)]
    public NetworkArray<Vector4> AvatarBoneRot => default;

    [Header("移动（传统 FPS 手感）")]
    public float moveSpeed = 3.5f;
    public float lookSpeedYaw = 2.0f;     // 鼠标左右（像素/帧 × 系数）
    public float lookSpeedPitch = 1.5f;   // 鼠标上下
    public float headHeight = 1.45f;      // yixuan3 VRM 眼高

    [Header("跳跃（只有按键才跳，无自动跳跃/攀爬）")]
    public float jumpImpulse = 5.5f;      // 起跳冲量（约 0.75m 高；NCC gravity=-20）
    public float jumpCooldown = 0.25f;    // 连跳保护(秒)

    NetworkCharacterController _cc;
    Camera _cam;
    PlayerChatController _chat;
    Animator _anim;
    readonly Transform[] _bones = new Transform[BONE_COUNT];

    // —— 自建 3D 语音通道 ——
    // 玩家ID → 化身根：远端客户端按说话人位置做 3D 播放
    public static readonly System.Collections.Generic.Dictionary<int, Transform> PlayerRegistry
        = new System.Collections.Generic.Dictionary<int, Transform>();
    public static NetworkPlayerSync LocalInstance;   // 本机玩家实例（主机转发小念语音用）

    // Update 采集的输入（FixedUpdateNetwork 执行）
    Vector3 _moveInput;
    float _yawInput;
    float _pitch;
    bool _jumpQueued;          // 按键按下 → 网络帧里执行 Jump()
    float _jumpCooldownLeft;   // 剩余冷却

    // VR 模式（有头显自动启用；学校头显按 OpenXR 通用）
    bool _vrActive;
    bool _vrJumpHeld;
    float _xrInitTimer;

    public override void Spawned()
    {
        _cc = GetComponent<NetworkCharacterController>();
        // 取消“自动攀爬/自动跳台阶”：Unity CharacterController 的 stepOffset 会在贴着
        // 台阶/家具边缘时自动把角色抬上去（看起来像自己攀爬、不按键就弹跳）。
        // 归零后只有按空格（Jump）才会离地。对旧预制体也生效，无需重新生成。
        var controller = GetComponent<CharacterController>();
        if (controller != null) controller.stepOffset = 0f;
        // 起跳冲量统一由本脚本控制（默认 5.5 ≈ 0.75m 高，不飞天）
        if (_cc != null) _cc.jumpImpulse = jumpImpulse;
        if (Object.HasStateAuthority)
        {
            LocalInstance = this;
            _cam = GetComponentInChildren<Camera>();
            if (_cam == null)
            {
                var go = new GameObject("PlayerCamera");
                go.transform.SetParent(transform, false);
                go.transform.localPosition = new Vector3(0f, headHeight, 0f);
                _cam = go.AddComponent<Camera>();
                go.AddComponent<AudioListener>();
            }
            _cam.transform.localPosition = new Vector3(0f, headHeight, 0f);
            _cam.gameObject.tag = "MainCamera";   // 供气泡公告板等 Camera.main 使用

            // 锁定鼠标（Esc 释放），转向手感才正常
            Cursor.lockState = CursorLockMode.Locked;
            Cursor.visible = false;

            // 本机聊天 UI：用玩家自带的 PlayerChatController（能直接找到场景里的小念）
            _chat = GetComponent<PlayerChatController>();
            if (_chat == null) _chat = gameObject.AddComponent<PlayerChatController>();

            // 关掉其它相机和聊天入口，避免多视角/多 UI 打架
            foreach (var other in FindObjectsOfType<Camera>())
                if (other != _cam) other.enabled = false;
            foreach (var other in FindObjectsOfType<PlayerChatController>())
                if (other != _chat) other.enabled = false;

            // VR 全身捕捉组件（头+双手；髋/脚追踪器自动发现）。桌面模式自动休眠。
            if (GetComponent<VrFullBodyTracking>() == null)
                gameObject.AddComponent<VrFullBodyTracking>();

            // 语音采集（按住说话 → RPC 到主机）。桌面模式同样可用（V 键）。
            var voice = GetComponent<VoiceCapture>();
            if (voice == null) voice = gameObject.AddComponent<VoiceCapture>();
            voice.sync = this;

            // 尝试初始化 XR（连着头显才成功；学校头显/Quest Link 均走 OpenXR）
            TryInitXr();
        }

        // 化身骨骼缓存（联网同步用；所有端都缓存）
        _anim = GetComponentInChildren<Animator>();
        CacheAvatarBones();

        // 自建语音通道注册：玩家ID → 化身根（供远端 3D 播放定位）
        if (Runner != null)
            PlayerRegistry[Runner.LocalPlayer.PlayerId] = transform;
    }

    void OnDestroy()
    {
        if (Runner != null)
            PlayerRegistry.Remove(Runner.LocalPlayer.PlayerId);
        if (LocalInstance == this)
            LocalInstance = null;
    }

    void CacheAvatarBones()
    {
        if (_anim == null || _anim.avatar == null || !_anim.avatar.isHuman) return;
        _bones[0] = _anim.GetBoneTransform(HumanBodyBones.Hips);
        _bones[1] = _anim.GetBoneTransform(HumanBodyBones.Spine);
        _bones[2] = _anim.GetBoneTransform(HumanBodyBones.Chest);
        _bones[3] = _anim.GetBoneTransform(HumanBodyBones.Neck);
        _bones[4] = _anim.GetBoneTransform(HumanBodyBones.Head);
        _bones[5] = _anim.GetBoneTransform(HumanBodyBones.LeftUpperArm);
        _bones[6] = _anim.GetBoneTransform(HumanBodyBones.LeftLowerArm);
        _bones[7] = _anim.GetBoneTransform(HumanBodyBones.LeftHand);
        _bones[8] = _anim.GetBoneTransform(HumanBodyBones.RightUpperArm);
        _bones[9] = _anim.GetBoneTransform(HumanBodyBones.RightLowerArm);
        _bones[10] = _anim.GetBoneTransform(HumanBodyBones.RightHand);
        _bones[11] = _anim.GetBoneTransform(HumanBodyBones.LeftUpperLeg);
        _bones[12] = _anim.GetBoneTransform(HumanBodyBones.LeftLowerLeg);
        _bones[13] = _anim.GetBoneTransform(HumanBodyBones.LeftFoot);
        _bones[14] = _anim.GetBoneTransform(HumanBodyBones.RightUpperLeg);
        _bones[15] = _anim.GetBoneTransform(HumanBodyBones.RightLowerLeg);
        _bones[16] = _anim.GetBoneTransform(HumanBodyBones.RightFoot);
    }

    void TryInitXr()
    {
        if (XRSettings.isDeviceActive) { _vrActive = true; return; }
        try
        {
            var manager = UnityEngine.XR.Management.XRGeneralSettings.Instance?.Manager;
            if (manager == null) return;
            // 注意：InitializeLoaderSync() 返回 void（XR Management 4.x），
            // 成功后用 activeLoader / StartSubsystems 启动，再按 isDeviceActive 判断。
            manager.InitializeLoaderSync();
            if (manager.activeLoader != null)
                manager.StartSubsystems();
            _vrActive = XRSettings.isDeviceActive;
            if (_vrActive) Debug.Log("[玩家] XR 已初始化，VR 模式启用（左摇杆移动 / 右手柄主键跳跃）");
        }
        catch (System.Exception e)
        {
            Debug.LogWarning("[玩家] XR 初始化失败（继续桌面模式）：" + e.Message);
        }
    }

    void Update()
    {
        if (!Object.HasStateAuthority) return;

        // VR：头显后插上也能自动切过去
        if (!_vrActive)
        {
            _xrInitTimer -= Time.deltaTime;
            if (_xrInitTimer <= 0f)
            {
                _xrInitTimer = 5f;
                TryInitXr();
            }
        }
        else
        {
            UpdateVrInput();
            return;   // VR 模式下跳过鼠标/锁光标逻辑
        }

        // Esc 释放鼠标；聊天框打开时不允许重新锁定（防"点发送后鼠标消失"）
        if (Input.GetKeyDown(KeyCode.Escape) && !PlayerChatController.ChatUiOpen)
        {
            Cursor.lockState = CursorLockMode.None;
            Cursor.visible = true;
        }
        if (Input.GetMouseButtonDown(0) && Cursor.lockState != CursorLockMode.Locked
            && !PlayerChatController.ChatUiOpen)
        {
            Cursor.lockState = CursorLockMode.Locked;
            Cursor.visible = false;
        }

        // 采集输入（不直接改 transform；移动在 FixedUpdateNetwork 执行）
        // A/D = 左右平移（不转身），W/S = 前/后，方向跟随当前朝向
        float h = Input.GetAxis("Horizontal");
        float v = Input.GetAxis("Vertical");
        Vector3 dir = transform.right * h + transform.forward * v;
        if (dir.magnitude > 1f) dir.Normalize();
        _moveInput = dir;

        // 空格跳跃（Update 采集 → FixedUpdateNetwork 执行，带冷却与落地判定）
        if (Input.GetKeyDown(KeyCode.Space))
            _jumpQueued = true;

        // 鼠标视角：与帧率无关（GetAxis("Mouse X") 已是每帧像素增量）
        float mx = Input.GetAxis("Mouse X");
        float my = Input.GetAxis("Mouse Y");
        _yawInput += mx * lookSpeedYaw;

        // 俯仰作用在相机子物体上（本地即可）
        if (_cam != null)
        {
            _pitch = Mathf.Clamp(_pitch - my * lookSpeedPitch, -60f, 60f);
            _cam.transform.localRotation = Quaternion.Euler(_pitch, 0f, 0f);
        }
    }

    // VR 输入：头显驱动视角/朝向，左摇杆移动（相对头朝向），右手柄主键(A)跳跃。
    // 所有头显都走 OpenXR 通用接口（Quest Link / SteamVR / 学校头显）。
    void UpdateVrInput()
    {
        var hmd = InputDevices.GetDeviceAtXRNode(XRNode.CenterEye);
        if (hmd.isValid &&
            hmd.TryGetFeatureValue(CommonUsages.centerEyePosition, out Vector3 headPos) &&
            hmd.TryGetFeatureValue(CommonUsages.centerEyeRotation, out Quaternion headRot))
        {
            if (_cam != null)
            {
                _cam.transform.localPosition = headPos;
                _cam.transform.localRotation = headRot;
            }
            // 身体朝向跟随头显偏航（头骨细调交给 VrFullBodyTracking）
            Vector3 fwd = headRot * Vector3.forward;
            fwd.y = 0f;
            if (fwd.sqrMagnitude > 0.001f)
                transform.rotation = Quaternion.LookRotation(fwd);
        }

        var lh = InputDevices.GetDeviceAtXRNode(XRNode.LeftHand);
        Vector2 stick = Vector2.zero;
        if (lh.isValid) lh.TryGetFeatureValue(CommonUsages.primary2DAxis, out stick);
        Vector3 dir = transform.forward * stick.y + transform.right * stick.x;
        if (dir.magnitude > 1f) dir.Normalize();
        _moveInput = dir;

        // 跳跃：右手柄主键（A / X）
        var rh = InputDevices.GetDeviceAtXRNode(XRNode.RightHand);
        bool btn = false;
        if (rh.isValid) rh.TryGetFeatureValue(CommonUsages.primaryButton, out btn);
        if (btn && !_vrJumpHeld)
            _jumpQueued = true;
        _vrJumpHeld = btn;
    }

    public override void FixedUpdateNetwork()
    {
        if (!Object.HasStateAuthority || _cc == null) return;
        _cc.Move(_moveInput * moveSpeed * Runner.DeltaTime);
        // 空格跳跃：冷却内/未落地不跳；只有主动按键才会离地（无自动跳跃）
        if (_jumpQueued)
        {
            _jumpQueued = false;
            if (_jumpCooldownLeft <= 0f && _cc.Grounded)
            {
                _cc.Jump();
                _jumpCooldownLeft = jumpCooldown;
            }
        }
        if (_jumpCooldownLeft > 0f)
            _jumpCooldownLeft -= Runner.DeltaTime;
        if (Mathf.Abs(_yawInput) > 0.001f)
        {
            transform.Rotate(0f, _yawInput, 0f);
            _yawInput = 0f;
        }
    }

    // ---------------- 化身骨骼联网同步 ----------------
    // 本机（权威端）：把 CSM/VR 捕捉写好的骨骼旋转读进网络属性，由 Fusion 增量同步；
    // 远端：用收到的数据驱动化身骨骼（别人能看到你的动捕/待机姿态）。
    void LateUpdate()
    {
        if (_bones[0] == null) return;
        if (Object.HasStateAuthority)
        {
            for (int i = 0; i < BONE_COUNT; i++)
            {
                var q = _bones[i].localRotation;
                AvatarBoneRot.Set(i, new Vector4(q.x, q.y, q.z, q.w));
            }
        }
        else
        {
            for (int i = 0; i < BONE_COUNT; i++)
            {
                var v = AvatarBoneRot.Get(i);
                if (v.x == 0f && v.y == 0f && v.z == 0f && v.w == 0f) continue;  // 尚无数据
                _bones[i].localRotation = new Quaternion(v.x, v.y, v.z, v.w);
            }
        }
    }

    // ---------------- 语音采集接收（学生 → 主机）----------------
    // 主机判定：本机桥已连接（BridgeHub.IsOpen）即视为主机；识别失败可勾 ForceHost。
    public static bool ForceHost;
    public static bool IsHostRig => ForceHost || (BridgeHub.Instance != null && BridgeHub.Instance.IsOpen);

    static readonly System.Collections.Generic.Dictionary<int, System.Collections.Generic.List<byte[]>> _voiceParts
        = new System.Collections.Generic.Dictionary<int, System.Collections.Generic.List<byte[]>>();
    static readonly System.Collections.Generic.Dictionary<int, int> _voiceTotal
        = new System.Collections.Generic.Dictionary<int, int>();

    /// <summary>语音分块 RPC（Fusion 代码生成器确认支持 byte[] 参数；4KB/块，可靠有序）。
    /// 所有端都会组装：主机 → 落盘 + 送桥做 ASR/声纹；其它端 → 在说话人化身位置 3D 播放。</summary>
    [Rpc(RpcSources.All, RpcTargets.All)]
    public void RPC_VoiceChunk(int playerId, int chunkIndex, int totalChunks, byte[] data)
    {
        if (!_voiceParts.TryGetValue(playerId, out var parts))
        {
            parts = new System.Collections.Generic.List<byte[]>();
            _voiceParts[playerId] = parts;
            _voiceTotal[playerId] = totalChunks;
        }
        while (parts.Count <= chunkIndex) parts.Add(null);
        parts[chunkIndex] = data;
        int got = 0;
        foreach (var p in parts) if (p != null) got++;
        if (got < _voiceTotal[playerId]) return;

        var wav = AssembleVoice(parts);
        _voiceParts.Remove(playerId);
        _voiceTotal.Remove(playerId);
        if (wav == null) return;

        // 主机：落盘（留档）+ 交给桥做 ASR + 声纹识别
        if (IsHostRig)
        {
            SaveVoiceWav(playerId, wav);
            if (BridgeHub.Instance != null && BridgeHub.Instance.IsOpen)
                BridgeHub.Instance.SendVoiceInput(playerId, System.Convert.ToBase64String(wav));
        }
        // 所有端（除说话人自己）：在说话人化身位置 3D 播放，实现"听到谁在哪说话"
        if (Runner != null && Runner.LocalPlayer.PlayerId != playerId)
            PlayVoiceAt(playerId, wav);
    }

    static byte[] AssembleVoice(System.Collections.Generic.List<byte[]> parts)
    {
        int len = 0;
        foreach (var p in parts) if (p != null) len += p.Length;
        if (len == 0) return null;
        var wav = new byte[len];
        int off = 0;
        foreach (var p in parts)
            if (p != null)
            {
                System.Array.Copy(p, 0, wav, off, p.Length);
                off += p.Length;
            }
        return wav;
    }

    /// <summary>在说话人化身位置用 3D AudioSource 播放（距离衰减/立体声定位）。</summary>
    void PlayVoiceAt(int playerId, byte[] wav)
    {
        try
        {
            if (!PlayerRegistry.TryGetValue(playerId, out var root) || root == null) return;
            var src = root.GetComponentInChildren<AudioSource>();
            if (src == null)
            {
                var go = new GameObject("Voice3D");
                go.transform.SetParent(root, false);
                go.transform.localPosition = new Vector3(0f, 1.45f, 0f);
                src = go.AddComponent<AudioSource>();
            }
            src.spatialBlend = 1f;
            src.minDistance = 0.5f;
            src.maxDistance = 20f;
            src.rolloffMode = AudioRolloffMode.Linear;
            src.PlayOneShot(WavUtil.ToAudioClip(wav));
        }
        catch (System.Exception e)
        {
            Debug.LogWarning("[语音] 3D 播放失败：" + e.Message);
        }
    }

    void SaveVoiceWav(int playerId, byte[] wav)
    {
        try
        {
            var dir = System.IO.Path.Combine(Application.persistentDataPath, "VoiceIn");
            System.IO.Directory.CreateDirectory(dir);
            var path = System.IO.Path.Combine(dir,
                $"voice_p{playerId}_{System.DateTime.Now:yyyyMMdd_HHmmss}.wav");
            System.IO.File.WriteAllBytes(path, wav);
            Debug.Log($"[语音] 已保存玩家 {playerId} 的语音：{path}");
        }
        catch (System.Exception e)
        {
            Debug.LogWarning("[语音] 保存失败：" + e.Message);
        }
    }

    // ---------------- 小念语音广播（主机 → 所有客户端）----------------
    /// <summary>主机把桥推来的小念语音转发给所有客户端；远端各自在本机 NPC 上 3D 播放。</summary>
    public static void BroadcastNpcAudio(byte[] wav)
    {
        var inst = LocalInstance;
        if (inst == null || !inst.Object.HasStateAuthority) return;
        if (!IsHostRig) return;   // 只有主机收到桥音频，才需要转发
        try { inst.RPC_NpcAudio(wav); } catch (System.Exception) { }
    }

    [Rpc(RpcSources.All, RpcTargets.All)]
    public void RPC_NpcAudio(byte[] wav)
    {
        if (IsHostRig) return;   // 主机已走本地播放路径，避免双播
        var npc = FindObjectOfType<NpcAgent>();
        if (npc != null) npc.PlayWavBytes(wav);
    }
}
