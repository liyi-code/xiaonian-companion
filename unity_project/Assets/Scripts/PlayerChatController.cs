// PlayerChatController.cs
// 按 T 弹出聊天输入框，把文字发给当前激活的 NPC（默认找最近的 NpcBridgeClient）。
// 挂在场景任意物体上（如 Main Camera 或一个空 GameObject）。
using System.Collections.Generic;
using System.Linq;
using UnityEngine;

public class PlayerChatController : MonoBehaviour
{
    [Header("按键")]
    public KeyCode chatKey = KeyCode.T;

    [Header("目标 NPC（留空则自动找最近的 NPC）")]
    public NpcBridgeClient targetNpc;
    public NpcAgent targetAgent;

    [Header("窗口样式")]
    public float windowWidth = 400f;
    public float windowHeight = 120f;

    private bool _showInput = false;
    private string _text = "";
    private string _hint = "";
    private GUIStyle _boxStyle;
    private GUIStyle _textFieldStyle;
    private GUIStyle _buttonStyle;

    void Update()
    {
        if (Input.GetKeyDown(chatKey) && !_showInput)
        {
            _showInput = true;
            _text = "";
            ResolveTarget();
        }
    }

    void OnGUI()
    {
        if (!_showInput) return;
        EnsureStyles();

        float x = (Screen.width - windowWidth) * 0.5f;
        float y = Screen.height - windowHeight - 40f;
        Rect rect = new Rect(x, y, windowWidth, windowHeight);

        GUI.Box(rect, $"跟小念说话 ({chatKey})", _boxStyle);
        GUILayout.BeginArea(new Rect(x + 10f, y + 30f, windowWidth - 20f, windowHeight - 40f));
        GUILayout.BeginHorizontal();
        GUI.SetNextControlName("ChatInput");
        _text = GUILayout.TextField(_text, _textFieldStyle, GUILayout.Height(32f), GUILayout.ExpandWidth(true));
        if (GUI.GetNameOfFocusedControl() != "ChatInput")
            GUI.FocusControl("ChatInput");

        if (GUILayout.Button("发送", _buttonStyle, GUILayout.Width(60f), GUILayout.Height(32f)))
            Submit();
        GUILayout.EndHorizontal();

        GUILayout.Space(6f);
        GUILayout.Label(_hint, new GUIStyle(GUI.skin.label) { normal = { textColor = Color.yellow } });
        GUILayout.EndArea();

        Event e = Event.current;
        if (e.type == EventType.KeyDown && e.keyCode == KeyCode.Return)
        {
            Submit();
            e.Use();
        }
        if (e.type == EventType.KeyDown && e.keyCode == KeyCode.Escape)
        {
            _showInput = false;
            e.Use();
        }
    }

    void Submit()
    {
        string t = _text.Trim();
        if (string.IsNullOrEmpty(t)) return;

        ResolveTarget();
        string receiverName = null;
        if (targetNpc != null)
        {
            Debug.Log($"[PlayerChat] 发送给 {targetNpc.npcId}: {t}");
            targetNpc.SendChat(t);
            receiverName = targetNpc.npcId;
        }
        else if (targetAgent != null)
        {
            Debug.Log($"[PlayerChat] 发送给 Agent {targetAgent.npcId}: {t}");
            targetAgent.SendChat(t);
            receiverName = targetAgent.npcId;
        }
        else
        {
            _hint = "场景里找不到带 NpcBridgeClient / NpcAgent 的 NPC。";
            Debug.LogWarning("[PlayerChat] 找不到目标 NPC/Agent");
            return;
        }

        _hint = $"→ {receiverName}: {t}";
        _text = "";
        _showInput = false;
    }

    void ResolveTarget()
    {
        if (targetNpc != null || targetAgent != null) return;

        var clients = FindObjectsOfType<NpcBridgeClient>();
        var agents = FindObjectsOfType<NpcAgent>();

        // 优先用 NpcBridgeClient；没有再用 NpcAgent
        if (clients.Length == 1) { targetNpc = clients[0]; return; }
        if (agents.Length == 1) { targetAgent = agents[0]; return; }

        Transform cam = Camera.main != null ? Camera.main.transform : transform;
        if (clients.Length > 0)
        {
            targetNpc = clients
                .OrderBy(n => Vector3.Distance(cam.position, n.transform.position))
                .FirstOrDefault();
            return;
        }
        if (agents.Length > 0)
        {
            targetAgent = agents
                .OrderBy(n => Vector3.Distance(cam.position, n.transform.position))
                .FirstOrDefault();
        }
    }

    void EnsureStyles()
    {
        if (_boxStyle != null) return;
        _boxStyle = new GUIStyle(GUI.skin.box);
        _boxStyle.fontSize = 14;
        _boxStyle.alignment = TextAnchor.UpperCenter;

        _textFieldStyle = new GUIStyle(GUI.skin.textField);
        _textFieldStyle.fontSize = 16;
        _textFieldStyle.alignment = TextAnchor.MiddleLeft;

        _buttonStyle = new GUIStyle(GUI.skin.button);
        _buttonStyle.fontSize = 14;
    }
}
