// VoiceCapture.cs —— 客户端语音采集（按住说话 → 分块 RPC → 主机保存/桥ASR/声纹）
// ============================================================================
// 挂在玩家根物体上（NetworkPlayerSync.Spawned 会自动添加并注入 sync 引用）。
// 操作：桌面按住 V 说话；VR 模式按住右手柄扳机说话（两者同时生效）。松开即分块发给主机。
// 主机侧：NetworkPlayerSync.RPC_VoiceChunk 组装完整后存 WAV + 送桥做 ASR/声纹识别。
//
// 关键修复（V 键没反应的常见根因）：
//   · 部分麦克风不支持 16kHz → Microphone.Start 一直返回 null。现在按
//     16000 → 48000 → 44100 依次尝试，非 16k 的自动重采样到 16k。
//   · 全程 Console 日志 + 屏幕「录音中」提示，任何环节出问题都看得见。
// ============================================================================
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.XR;

public class VoiceCapture : MonoBehaviour
{
    [Header("接线（由 NetworkPlayerSync 自动注入）")]
    public NetworkPlayerSync sync;

    [Header("桌面按键说话")]
    public KeyCode talkKey = KeyCode.V;

    [Header("录音参数")]
    public int targetRate = 16000;      // 发给桥的采样率（ASR 友好）
    public int micIndex = 0;            // 麦克风设备序号（0=第一个）
    public float minDuration = 0.3f;    // 短于此时长的误触不发送

    AudioClip _clip;
    int _srcRate;                       // 实际录音采样率（16000/48000/44100）
    bool _recording;
    int _clipPos;
    float _startTime;
    float _micRetry;
    string _devName = "";
    readonly List<float> _samples = new List<float>();

    void Start()
    {
        TryStartMic();
    }

    string DeviceName()
    {
        if (Microphone.devices.Length == 0) return null;
        return Microphone.devices[Mathf.Min(micIndex, Microphone.devices.Length - 1)];
    }

    void TryStartMic()
    {
        if (_clip != null) return;
        if (Microphone.devices.Length == 0)
        {
            Debug.LogWarning("[语音] 未找到麦克风，语音采集不可用（检查 Windows 设置→隐私→麦克风）");
            enabled = false;
            return;
        }
        _devName = DeviceName();
        // 采样率兼容链：优先 16k（省带宽），设备不支持则 48k / 44.1k，发送前重采样
        foreach (int rate in new[] { 16000, 48000, 44100 })
        {
            _clip = Microphone.Start(_devName, true, 1, rate);
            if (_clip != null)
            {
                _srcRate = rate;
                break;
            }
        }
        if (_clip == null)
        {
            Debug.LogWarning("[语音] 麦克风打开失败，0.5 秒后重试…（设备可能被占用）");
            _micRetry = 0.5f;
            return;
        }
        Debug.Log($"[语音] 麦克风就绪：{_devName} @ {_srcRate}Hz（按住 {talkKey} 说话 / VR 扳机）");
        if (Microphone.devices.Length > 1)
        {
            var names = string.Join(" | ", Microphone.devices);
            Debug.Log($"[语音] 可用麦克风列表：{names}（当前用序号 {micIndex}，可在 VoiceCapture 上改）");
        }
    }

    bool PushToTalk()
    {
        // 桌面 V 键与 VR 扳机【同时生效】（取或）：没戴头显时按 V 一定有效
        bool vrTrig = false;
        if (XRSettings.isDeviceActive)
        {
            var rh = InputDevices.GetDeviceAtXRNode(XRNode.RightHand);
            if (rh.isValid) rh.TryGetFeatureValue(CommonUsages.triggerButton, out vrTrig);
        }
        return Input.GetKey(talkKey) || vrTrig;
    }

    void Update()
    {
        if (sync == null) sync = GetComponent<NetworkPlayerSync>();
        if (_clip == null)
        {
            _micRetry -= Time.deltaTime;
            if (_micRetry <= 0f) TryStartMic();
            return;
        }

        bool talk = PushToTalk();
        if (talk && !_recording)
        {
            _recording = true;
            _samples.Clear();
            _clipPos = Microphone.GetPosition(_devName);
            _startTime = Time.time;
            Debug.Log("[语音] 开始录音…");
        }

        if (_recording)
        {
            // 环形 1s 缓冲：把新录到的样本读进列表
            int pos = Microphone.GetPosition(_devName);
            int n = pos >= _clipPos ? pos - _clipPos : (_clip.samples - _clipPos) + pos;
            if (n > 0)
            {
                var buf = new float[n];
                _clip.GetData(buf, _clipPos);
                _samples.AddRange(buf);
                _clipPos = pos;
            }
        }

        if (_recording && !talk)
        {
            _recording = false;
            float dur = Time.time - _startTime;
            if (dur >= minDuration && _samples.Count > 0)
            {
                if (sync != null) SendSamples();
                else Debug.LogWarning("[语音] 未接 NetworkPlayerSync，语音未发送");
            }
            else
            {
                Debug.Log($"[语音] 录音太短({dur:F1}s)或为空，已忽略");
            }
        }
    }

    void OnGUI()
    {
        if (_recording)
        {
            var style = new GUIStyle(GUI.skin.box) { fontSize = 16, alignment = TextAnchor.MiddleCenter };
            GUI.Box(new Rect(Screen.width / 2f - 110, Screen.height - 100, 220, 44),
                    "🎤 录音中…（松开结束）", style);
        }
    }

    void SendSamples()
    {
        // 重采样到 targetRate（48k→16k 抽 1/3；44.1k 按比例最近邻采样）
        byte[] pcm = ResampleToPcm16(_samples, _srcRate, targetRate);
        // 448 字节/块：Fusion 2 RPC 载荷上限 512 字节（含参数），留足余量
        const int CHUNK = 448;
        int total = Mathf.CeilToInt(pcm.Length / (float)CHUNK);
        int playerId = sync.Runner != null ? sync.Runner.LocalPlayer.PlayerId : -1;
        for (int i = 0; i < total; i++)
        {
            int len = Mathf.Min(CHUNK, pcm.Length - i * CHUNK);
            var part = new byte[len];
            System.Array.Copy(pcm, i * CHUNK, part, 0, len);
            sync.RPC_VoiceChunk(playerId, i, total, part);
        }
        Debug.Log($"[语音] 已发送 {_samples.Count / (float)_srcRate:F1}s 语音（{total} 块，{_srcRate}→{targetRate}Hz）");
    }

    static byte[] ResampleToPcm16(List<float> src, int srcRate, int dstRate)
    {
        if (srcRate <= 0 || dstRate <= 0) return new byte[0];
        if (srcRate == dstRate) return ToPcm16(src);
        int n = (int)(src.Count * ((float)dstRate / srcRate));
        if (n <= 0) return new byte[0];
        var outF = new List<float>(n);
        for (int i = 0; i < n; i++)
        {
            int si = (int)(i * ((float)srcRate / dstRate));
            if (si >= src.Count) si = src.Count - 1;
            outF.Add(src[si]);
        }
        return ToPcm16(outF);
    }

    static byte[] ToPcm16(List<float> samples)
    {
        var pcm = new byte[samples.Count * 2];
        int idx = 0;
        foreach (var s in samples)
        {
            short v = (short)(Mathf.Clamp(s, -1f, 1f) * 32767f);
            pcm[idx++] = (byte)(v & 0xFF);
            pcm[idx++] = (byte)((v >> 8) & 0xFF);
        }
        return pcm;
    }
}
