/*
 * 【已废弃 / 单 NPC 旧版】
 * 本文件是「单 NPC」时代的客户端桥。多 NPC 版本请改用：
 *   - BridgeHub.cs    （单一 WebSocket 连接 + 按 npc_id 路由）
 *   - NpcManager.cs   （管理多个 NPC 的 VRM 实例）
 *   - NpcAgent.cs     （单个 NPC 的行为：对话/表情/动作/语音/任务）
 *   - SymbolicPerception.cs / AgentController.cs
 * 保留本文件仅作协议参考，不要再挂到场景里（会和 BridgeHub 抢同一个 WebSocket 端口）。
 *
 * ───────────────────────── 旧版说明（单 NPC）─────────────────────────
 * 小念 ⇄ Unity(VRM) 客户端桥。
 *
 * 依赖：
 *   1) UniVRM（VRM0）：https://github.com/vrm-c/UniVRM  （导入后可用 VRMBlendShapeProxy / BlendShapePreset）
 *   2) WebSocketSharp：把 WebSocketSharp.cs 拖进 Assets（或 Package Manager 装 com.detonator.websocket-sharp）
 *
 * 用法：
 *   - 把本脚本挂到场景里带 VRMBlendShapeProxy 的模型 GameObject 上（同物体需有 Animator + AudioSource）。
 *   - 在 Inspector 里拖好 uiInput / uiSendButton / uiSubtitle / audioSource / animator（可选）。
 *   - 运行后点「连接」，在输入框打字回车即可；小念会流式出字、出声、做表情与动作。
 *
 * 事件协议（与 src/bridge.py 对应）：
 *  [Unity -> Python]
 *   user_input       -> 玩家打字输入
 *   world_load       -> 区域(预加载)加载/卸载：{region_id, loaded}
 *   symbolic_percept -> 符号感知批量：{agent_pos, objects:[{id,name,type,pos,state,region}]}（无图像）
 *   visual_snapshot   -> 低频 1080p 视觉快照：{cam_pos, image_b64}
 *  [Python -> Unity]
 *   token        -> 字幕增量
 *   speech_start -> 开始说话（启用口型）
 *   audio        -> base64 wav 字节，播放并实时口型
 *   emotion      -> dominant: joy/anger/sadness/calm/anxiety -> 表情 Blendshape
 *   action       -> name: jump/turn/wave/pat/nod -> Animator 触发器
 *   talk_stop    -> 结束说话（口型归零）
 *   agent_command-> 主动探索指令：{action:move/look/interact/wander, target:{x,y,z}, object_id}
 *   agent_thought-> 小念内心独白(不念出声)：{text}
 *   ready        -> 连接成功，含 name / voice_ready
 */

using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.UI;
using WebSocketSharp;
using VRM;

[Serializable]
public class Vec3
{
    public float x, y, z;
    public Vector3 ToVector3() { return new Vector3(x, y, z); }
}

// 一个被小念“符号感知”到的物体（无图像，只有结构化文本 + 坐标）
[Serializable]
public class PerceptObject
{
    public string id;        // 物体唯一 id（场景内稳定）
    public string name;      // 显示名
    public string type;      // 类型(决定兴趣/可交互)：chest/npc/door/...
    public Vector3 pos;      // 世界坐标
    public string state = "";        // 状态(可选)：open/closed/lit/...
    public string region = "";      // 所属区域(预加载)；不填视为当前区域
}

[Serializable]
public class BridgeEvent
{
    public string type;
    public string text;
    public string dominant;
    public string name;
    public string wav;          // base64
    // 世界感知 / 主动探索相关
    public string action;       // agent_command: move/look/interact/wander
    public string object_id;
    public Vec3 target;         // 目标坐标
}

public class XiaonianBridge : MonoBehaviour
{
    [Header("连接")]
    public string wsUrl = "ws://127.0.0.1:8765";

    [Header("UI（可选）")]
    public InputField uiInput;
    public Button uiSendButton;
    public Text uiSubtitle;
    public Text uiThought;      // 小念“内心独白”气泡（agent_thought，不念出声）

    [Header("模型驱动")]
    public AudioSource audioSource;
    public Animator animator;
    public VRMBlendShapeProxy blendShapeProxy;

    private AgentController agentCtrl;

    [Header("口型")]
    [Tooltip("音频能量 -> 嘴型开口的增益；按模型/音量微调")]
    public float mouthScale = 2.2f;
    [Tooltip("口型平滑系数 0~1，越大越平滑")]
    public float mouthSmooth = 0.3f;

    private WebSocket ws;
    private ConcurrentQueue<BridgeEvent> eventQueue = new ConcurrentQueue<BridgeEvent>();

    // 口型状态
    private bool lipSyncActive = false;
    private float[] clipSamples;     // 当前播放 clip 的单声道 PCM 缓存
    private int clipFreq;
    private float mouthOpen = 0f;

    // 表情预设映射
    private static readonly Dictionary<string, BlendShapePreset> EmotionMap =
        new Dictionary<string, BlendShapePreset>
    {
        { "joy", BlendShapePreset.Joy },
        { "anger", BlendShapePreset.Angry },
        { "sadness", BlendShapePreset.Sorrow },
        { "anxiety", BlendShapePreset.Sorrow },
        { "calm", BlendShapePreset.Neutral },
    };
    private BlendShapePreset[] allPresets = new BlendShapePreset[]
    {
        BlendShapePreset.Joy, BlendShapePreset.Angry, BlendShapePreset.Sorrow,
        BlendShapePreset.Fun, BlendShapePreset.Neutral,
    };

    void Start()
    {
        if (blendShapeProxy == null) blendShapeProxy = GetComponent<VRMBlendShapeProxy>();
        if (audioSource == null) audioSource = GetComponent<AudioSource>();
        if (animator == null) animator = GetComponent<Animator>();
        agentCtrl = GetComponent<AgentController>();

        if (uiSendButton != null) uiSendButton.onClick.AddListener(() => SendFromInput());
        if (uiInput != null)
        {
            uiInput.onEndEdit.AddListener((s) =>
            {
                if (Input.GetKeyDown(KeyCode.Return)) SendFromInput();
            });
        }

        Connect();
    }

    void OnDestroy()
    {
        if (ws != null)
        {
            ws.OnMessage -= OnWsMessage;
            ws.Close();
        }
    }

    // ---------------- WebSocket ----------------
    public void Connect()
    {
        ws = new WebSocket(wsUrl);
        ws.OnOpen += (s, e) => Debug.Log("[小念] 已连接 " + wsUrl);
        ws.OnError += (s, e) => Debug.LogError("[小念] 错误 " + e.Message);
        ws.OnClose += (s, e) => Debug.Log("[小念] 断开");
        ws.OnMessage += OnWsMessage;
        ws.Connect();
    }

    private void OnWsMessage(object sender, MessageEventArgs e)
    {
        if (!e.IsText) return;
        BridgeEvent ev;
        try { ev = JsonUtility.FromJson<BridgeEvent>(e.Data); }
        catch { return; }
        if (ev == null) return;
        // WebSocketSharp 的回调在后台线程；统一丢进队列，由主线程 Update 消费
        eventQueue.Enqueue(ev);
    }

    public void SendUserInput(string text)
    {
        if (ws == null || ws.ReadyState != WebSocketState.Open) return;
        if (string.IsNullOrWhiteSpace(text)) return;
        var msg = "{\"type\":\"user_input\",\"text\":" + Js(text) + "}";
        ws.Send(msg);
    }

    // JSON 字符串转义（注意：JsonUtility.ToJson 不能序列化 string 基元，会返回 "{}"，
    // 之前用它拼 JSON 是 bug —— 所有出站消息都成非法 JSON。这里手写标准转义。）
    private static string Js(string s)
    {
        if (s == null) return "\"\"";
        var sb = new StringBuilder("\"");
        foreach (char c in s)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                    else sb.Append(c);
                    break;
            }
        }
        sb.Append("\"");
        return sb.ToString();
    }

    private void SendFromInput()
    {
        if (uiInput == null) return;
        var t = uiInput.text.Trim();
        uiInput.text = "";
        SendUserInput(t);
    }

    // ---------------- 世界感知：向外推送符号信息（无图像） ----------------
    public void SendWorldLoad(string regionId, bool loaded)
    {
        if (ws == null || ws.ReadyState != WebSocketState.Open) return;
        ws.Send("{\"type\":\"world_load\",\"region_id\":" + Js(regionId)
                + ",\"loaded\":" + (loaded ? "true" : "false") + "}");
    }

    public void SendSymbolicPercept(Vector3 agentPos, List<PerceptObject> objs)
    {
        if (ws == null || ws.ReadyState != WebSocketState.Open) return;
        var sb = new System.Text.StringBuilder();
        sb.Append("{\"type\":\"symbolic_percept\",\"agent_pos\":");
        sb.Append(Vec3From(agentPos));
        sb.Append(",\"objects\":[");
        for (int i = 0; i < objs.Count; i++)
        {
            var o = objs[i];
            if (i > 0) sb.Append(",");
            sb.Append("{\"id\":").Append(Js(o.id))
              .Append(",\"name\":").Append(Js(o.name))
              .Append(",\"type\":").Append(Js(o.type))
              .Append(",\"pos\":").Append(Vec3From(o.pos));
            if (!string.IsNullOrEmpty(o.state))
                sb.Append(",\"state\":").Append(Js(o.state));
            if (!string.IsNullOrEmpty(o.region))
                sb.Append(",\"region\":").Append(Js(o.region));
            sb.Append("}");
        }
        sb.Append("]}");
        ws.Send(sb.ToString());
    }

    public void SendVisualSnapshot(string b64)
    {
        if (ws == null || ws.ReadyState != WebSocketState.Open) return;
        // 1080p 截图以 base64 推给 Python，由其对视觉 API 做“结合符号感知”的推理
        var sb = new System.Text.StringBuilder();
        sb.Append("{\"type\":\"visual_snapshot\",\"cam_pos\":")
          .Append(Vec3From(transform.position))
          .Append(",\"image_b64\":").Append(Js(b64)).Append("}");
        ws.Send(sb.ToString());
    }

    private static string Vec3From(Vector3 p)
    {
        return "{\"x\":" + p.x.ToString("F2") + ",\"y\":" + p.y.ToString("F2")
             + ",\"z\":" + p.z.ToString("F2") + "}";
    }

    // ---------------- 主线程消费事件 ----------------
    void Update()
    {
        while (eventQueue.TryDequeue(out BridgeEvent ev))
        {
            HandleEvent(ev);
        }

        // 实时口型：跟随当前播放的音频能量
        if (lipSyncActive && audioSource != null && audioSource.isPlaying && clipSamples != null && clipSamples.Length > 0)
        {
            float t = audioSource.time;
            int idx = Mathf.Clamp((int)(t * clipFreq), 0, clipSamples.Length - 1);
            // 取当前帧附近小窗的能量
            float sum = 0f, n = 0f;
            for (int k = 0; k < 512 && idx + k < clipSamples.Length; k++)
            {
                sum += Mathf.Abs(clipSamples[idx + k]);
                n += 1f;
            }
            float amp = n > 0 ? sum / n : 0f;
            float target = Mathf.Clamp(amp * mouthScale, 0f, 1f);
            mouthOpen += (target - mouthOpen) * (1f - mouthSmooth);
            SetMouth(mouthOpen);
        }
        else if (!lipSyncActive && mouthOpen > 0.001f)
        {
            mouthOpen += (0f - mouthOpen) * 0.2f;
            SetMouth(mouthOpen);
        }
    }

    private void HandleEvent(BridgeEvent ev)
    {
        switch (ev.type)
        {
            case "ready":
                Debug.Log("[小念] 就绪 name=" + ev.name);
                break;

            case "token":
                if (uiSubtitle != null) uiSubtitle.text += ev.text;
                break;

            case "speech_start":
                lipSyncActive = true;
                if (uiSubtitle != null) uiSubtitle.text = "";   // 新一轮对话清屏
                break;

            case "emotion":
                SetEmotion(ev.dominant);
                break;

            case "action":
                if (animator != null) animator.SetTrigger(ev.name);
                break;

            case "audio":
                PlayAudio(ev.wav);
                break;

            case "talk_stop":
                lipSyncActive = false;
                SetMouth(0f);
                break;

            case "tool":
                Debug.Log("[小念][tool] " + ev.name + " -> " + ev.text);
                break;

            // ---- 主动探索指令（来自 Python 端 AutonomousExplorer）----
            case "agent_command":
                if (agentCtrl != null) agentCtrl.HandleCommand(ev.action, ev.target, ev.object_id);
                else Debug.Log("[小念] 收到 agent_command 但没有 AgentController：" + ev.action);
                break;

            // ---- 小念“内心独白”（不念出声，仅想法气泡）----
            case "agent_thought":
                if (uiThought != null)
                {
                    uiThought.text = ev.text;
                    uiThought.enabled = true;
                    CancelInvoke("HideThought");
                    Invoke("HideThought", 4f);
                }
                Debug.Log("[小念][想法] " + ev.text);
                break;
        }
    }

    private void HideThought() { if (uiThought != null) uiThought.enabled = false; }

    // ---------------- 语音播放 + 口型数据 ----------------
    private void PlayAudio(string b64)
    {
        try
        {
            byte[] wav = Convert.FromBase64String(b64);
            AudioClip clip = WavToAudioClip(wav, "xiaonian");
            if (clip == null || audioSource == null) return;
            audioSource.clip = clip;
            audioSource.Play();
            // 缓存单声道 PCM 供口型读取
            clipSamples = new float[clip.samples * clip.channels];
            clip.GetData(clipSamples, 0);
            if (clip.channels > 1)
            {
                // 简单取左声道
                float[] mono = new float[clip.samples];
                for (int i = 0; i < clip.samples; i++) mono[i] = clipSamples[i * clip.channels];
                clipSamples = mono;
            }
            clipFreq = clip.frequency;
        }
        catch (Exception ex)
        {
            Debug.LogError("[小念] 音频解码失败：" + ex.Message);
        }
    }

    // 极简 WAV(16-bit PCM) -> AudioClip
    private AudioClip WavToAudioClip(byte[] data, string name)
    {
        if (data == null || data.Length < 44) return null;
        int channels = BitConverter.ToInt16(data, 22);
        int freq = BitConverter.ToInt32(data, 24);
        int bits = BitConverter.ToInt16(data, 34);
        if (bits != 16) { Debug.LogError("[小念] 仅支持 16-bit PCM wav"); return null; }
        int dataPos = 12;
        while (dataPos + 4 < data.Length &&
               !(data[dataPos] == 'd' && data[dataPos + 1] == 'a' && data[dataPos + 2] == 't' && data[dataPos + 3] == 'a'))
        {
            int sz = BitConverter.ToInt32(data, dataPos + 4);
            dataPos += 8 + sz;
        }
        dataPos += 8;
        int sampleCount = (data.Length - dataPos) / 2;
        float[] samples = new float[sampleCount];
        for (int i = 0; i < sampleCount; i++)
        {
            short s = BitConverter.ToInt16(data, dataPos + i * 2);
            samples[i] = s / 32768f;
        }
        AudioClip clip = AudioClip.Create(name, sampleCount / channels, channels, freq, false);
        clip.SetData(samples, 0);
        return clip;
    }

    // ---------------- 表情 / 口型 ----------------
    private void SetEmotion(string key)
    {
        if (blendShapeProxy == null) return;
        foreach (var p in allPresets) blendShapeProxy.SetValue(p, 0f);
        if (!string.IsNullOrEmpty(key) && EmotionMap.TryGetValue(key, out var preset))
            blendShapeProxy.SetValue(preset, 1f);
        blendShapeProxy.Apply();
    }

    private void SetMouth(float v)
    {
        if (blendShapeProxy == null) return;
        // VRM 说话口型常用 A(aa) 形；你也可改映射为自定义口型
        blendShapeProxy.SetValue(BlendShapePreset.A, v);
        blendShapeProxy.Apply();
    }
}
