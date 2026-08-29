// VoiceCapture.cs —— 客户端语音采集（按住说话 → 分块 RPC → 主机保存，供 ASR 使用）
// ============================================================================
// 挂在玩家根物体上（NetworkPlayerSync.Spawned 会自动添加并注入 sync 引用）。
// 操作：桌面按住 V 说话；VR 模式按住右手柄扳机说话。松开即分块发给主机。
// 主机侧：NetworkPlayerSync.RPC_VoiceChunk 组装完整后存为
//   %persistentDataPath%/VoiceIn/voice_p<playerId>_<时间>.wav（16kHz 16bit 单声道 PCM）
// 下一步把该目录接 ASR（转文字 → 桥 user_input）即可让小念实时听到训练指令。
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
    public int sampleRate = 16000;      // ASR 友好采样率
    public int micIndex = 0;            // 麦克风设备序号
    public float minDuration = 0.3f;    // 短于此时长的误触不发送

    AudioClip _clip;
    bool _recording;
    int _clipPos;
    float _startTime;
    float _micRetry;
    readonly List<float> _samples = new List<float>();

    void Start()
    {
        TryStartMic();
    }

    void TryStartMic()
    {
        if (_clip != null) return;
        if (Microphone.devices.Length == 0)
        {
            Debug.LogWarning("[语音] 未找到麦克风，语音采集不可用");
            enabled = false;
            return;
        }
        int idx = Mathf.Min(micIndex, Microphone.devices.Length - 1);
        _clip = Microphone.Start(Microphone.devices[idx], true, 1, sampleRate);
        if (_clip == null)
            _micRetry = 0.5f;   // 设备未就绪，稍后重试
        else
            Debug.Log($"[语音] 麦克风就绪：{Microphone.devices[idx]}（按住 {talkKey} 说话 / VR 扳机）");
    }

    bool PushToTalk()
    {
        // VR：右手柄扳机；桌面：按住 talkKey
        if (XRSettings.isDeviceActive)
        {
            var rh = InputDevices.GetDeviceAtXRNode(XRNode.RightHand);
            bool trig = false;
            if (rh.isValid) rh.TryGetFeatureValue(CommonUsages.triggerButton, out trig);
            return trig;
        }
        return Input.GetKey(talkKey);
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
            _clipPos = Microphone.GetPosition(null);
            _startTime = Time.time;
        }

        if (_recording)
        {
            // 环形 1s 缓冲：把新录到的样本读进列表
            int pos = Microphone.GetPosition(null);
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
            if (Time.time - _startTime >= minDuration && _samples.Count > 0)
            {
                if (sync != null) SendSamples();
                else Debug.LogWarning("[语音] 未接 NetworkPlayerSync，语音未发送");
            }
        }
    }

    void SendSamples()
    {
        // float → 16bit PCM
        var pcm = new byte[_samples.Count * 2];
        int idx = 0;
        foreach (var s in _samples)
        {
            short v = (short)(Mathf.Clamp(s, -1f, 1f) * 32767f);
            pcm[idx++] = (byte)(v & 0xFF);
            pcm[idx++] = (byte)((v >> 8) & 0xFF);
        }

        // 4KB 分块（Fusion RPC 可靠有序；块大小远低于 RPC 载荷上限）
        const int CHUNK = 4096;
        int total = Mathf.CeilToInt(pcm.Length / (float)CHUNK);
        int playerId = sync.Runner != null ? sync.Runner.LocalPlayer.PlayerId : -1;
        for (int i = 0; i < total; i++)
        {
            int len = Mathf.Min(CHUNK, pcm.Length - i * CHUNK);
            var part = new byte[len];
            System.Array.Copy(pcm, i * CHUNK, part, 0, len);
            sync.RPC_VoiceChunk(playerId, i, total, part);
        }
        Debug.Log($"[语音] 已发送 {_samples.Count / (float)sampleRate:F1}s 语音（{total} 块）给主机");
    }
}
