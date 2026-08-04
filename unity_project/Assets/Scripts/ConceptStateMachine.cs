// ConceptStateMachine.cs
// 接 Python 推来的「多念竞争」概念与动作关键词，驱动 Animator：
//   - 主念概念 name -> 触发 Animator Trigger（如 "探索""敌意""好奇"）；
//   - action 关键词 -> 触发对应动作 Trigger（如 "挥手""摇头""点头"）；
//   - 语音播放时：用 AudioSource 的频谱能量驱动身体起伏（呼吸/说话律动）。
// 另含 ProximitySensor 逻辑：玩家进入 3 米 -> 发 stimuli 给 Python，并转头 LookAt 玩家。
//
// 说明：Animator 里需自建对应 Trigger 参数（探索/敌意/好奇/挥手/摇头/点头 等）；
//       没有对应 Trigger 时静默忽略，不影响其他功能。

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

    void Awake()
    {
        _anim = GetComponent<Animator>();
        _audio = GetComponent<AudioSource>();
        _bridge = GetComponent<NpcBridgeClient>();
    }

    // ---- 由 NpcBridgeClient 调用 ----
    public void TriggerConcept(string name, float weight)
    {
        if (_anim == null || string.IsNullOrEmpty(name)) return;
        // 概念名可能带空格/中文，Unity Trigger 名用 ASCII 更稳定：这里直接尝试，失败静默
        try { _anim.SetTrigger(Sanitize(name)); } catch { }
        // 概念强度也可驱动一个 Float 参数（若 Animator 有）
        try { _anim.SetFloat("ConceptWeight", weight); } catch { }
    }

    public void TriggerAction(string action)
    {
        if (_anim == null || string.IsNullOrEmpty(action)) return;
        // 动作意图形如 [ACT_IDLE] / [ACT_WAVE]，提取 ACT_xxx 作为 Animator Trigger 名（保留大小写）。
        // 不要走 Sanitize（否则 ACT_IDLE -> actidle，与 Animator 里建的 Trigger 对不上）。
        if (action.StartsWith("[ACT_") && action.EndsWith("]"))
        {
            string trigger = action.Substring(1, action.Length - 2); // -> ACT_IDLE
            try { _anim.SetTrigger(trigger); } catch { }
            return;
        }
        // 兜底：不带括号的写法（如外部直接传 ACT_IDLE）原样触发
        try { _anim.SetTrigger(action); } catch { }
    }

    public void OnSpeechStart() { try { _anim.SetBool("IsTalking", true); } catch { } }
    public void OnSpeechStop() { try { _anim.SetBool("IsTalking", false); } catch { } }

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
            if (_playerNear) FacePlayer();
        }

        // 说话律动：用音频频谱能量驱动身体起伏
        if (_audio != null && _audio.isPlaying)
        {
            float energy = GetAudioEnergy(_audio);
            _bobPhase += Time.deltaTime * (4f + energy * 8f); // 语速越快起伏越快
            float bob = Mathf.Sin(_bobPhase) * bodyBobScale * (0.3f + energy);
            transform.position += Vector3.up * bob * Time.deltaTime * 6f;
            // 也可驱动 Animator Float 表现呼吸/说话强度
            try { _anim.SetFloat("VoiceEnergy", energy); } catch { }
        }
    }

    void FacePlayer()
    {
        Vector3 dir = (player.position - transform.position);
        dir.y = 0;
        if (dir == Vector3.zero) return;
        Quaternion target = Quaternion.LookRotation(dir);
        transform.rotation = Quaternion.Slerp(transform.rotation, target, lookSpeed * Time.deltaTime);
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
