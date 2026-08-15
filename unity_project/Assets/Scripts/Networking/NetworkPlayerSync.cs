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

[RequireComponent(typeof(NetworkObject))]
[RequireComponent(typeof(NetworkCharacterController))]
public class NetworkPlayerSync : NetworkBehaviour
{
    [Header("移动（传统 FPS 手感）")]
    public float moveSpeed = 3.5f;
    public float lookSpeedYaw = 2.0f;     // 鼠标左右（像素/帧 × 系数）
    public float lookSpeedPitch = 1.5f;   // 鼠标上下
    public float headHeight = 1.45f;      // yixuan3 VRM 眼高

    NetworkCharacterController _cc;
    Camera _cam;
    PlayerChatController _chat;

    // Update 采集的输入（FixedUpdateNetwork 执行）
    Vector3 _moveInput;
    float _yawInput;
    float _pitch;

    public override void Spawned()
    {
        _cc = GetComponent<NetworkCharacterController>();
        if (Object.HasStateAuthority)
        {
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
        }
    }

    void Update()
    {
        if (!Object.HasStateAuthority) return;

        // Esc 释放鼠标（聊天时用）
        if (Input.GetKeyDown(KeyCode.Escape))
        {
            Cursor.lockState = CursorLockMode.None;
            Cursor.visible = true;
        }
        if (Input.GetMouseButtonDown(0) && Cursor.lockState != CursorLockMode.Locked)
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

    public override void FixedUpdateNetwork()
    {
        if (!Object.HasStateAuthority || _cc == null) return;
        _cc.Move(_moveInput * moveSpeed * Runner.DeltaTime);
        if (Mathf.Abs(_yawInput) > 0.001f)
        {
            transform.Rotate(0f, _yawInput, 0f);
            _yawInput = 0f;
        }
    }
}
