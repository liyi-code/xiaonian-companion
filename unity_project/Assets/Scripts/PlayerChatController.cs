// PlayerChatController.cs
// 按 T 弹出聊天输入框，把文字发给当前激活的 NPC（默认找最近的 NpcBridgeClient）。
// 挂法：随玩家预制体生成（联机）或挂在任意物体上（单机调试）。
// 2026-08 修订：
//   · 回车/小键盘回车在控件绘制前拦截（IMGUI 输入框会吃掉 Return，导致按回车没反应）
//   · 聊天框打开时自动释放鼠标，关闭时自动重新锁定（不再出现"点发送后鼠标消失"）
//   · 桥未连接时给出明确提示（需先运行 python -m src.bridge）
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

    /// <summary>聊天输入框是否打开（供玩家移动脚本判断要不要锁定鼠标）</summary>
    public static bool ChatUiOpen { get; private set; }

    private bool _showInput = false;
    private string _text = "";
    private string _hint = "";
    private GUIStyle _boxStyle;
    private GUIStyle _textFieldStyle;
    private GUIStyle _buttonStyle;

    void Update()
    {
        if (Input.GetKeyDown(chatKey))
        {
            if (!_showInput)
            {
                _showInput = true;
                _text = "";
                _hint = "";
                ResolveTarget();
                ReleaseCursor();
            }
            else
            {
                CloseChat();
            }
        }
    }

    void OnGUI()
    {
        if (!_showInput) return;
        EnsureStyles();

        // 关键：回车/ESC 必须在绘制控件【之前】拦截——
        // IMGUI 的 TextField 获得焦点后会把 Return 事件吃掉，绘制后再检查永远收不到。
        Event e = Event.current;
        if (e.type == EventType.KeyDown &&
            (e.keyCode == KeyCode.Return || e.keyCode == KeyCode.KeypadEnter))
        {
            Submit();
            e.Use();
            return;
        }
        if (e.type == EventType.KeyDown && e.keyCode == KeyCode.Escape)
        {
            CloseChat();
            e.Use();
            return;
        }

        float x = (Screen.width - windowWidth) * 0.5f;
        float y = Screen.height - windowHeight - 40f;

        GUI.Box(new Rect(x, y, windowWidth, windowHeight), $"跟小念说话 ({chatKey})", _boxStyle);
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
    }

    void Submit()
    {
        string t = _text.Trim();
        if (string.IsNullOrEmpty(t)) return;

        // 桥没连（Python 大脑没跑）时明确提示，别再静默吞消息
        bool bridgeOk = BridgeHub.Instance != null && BridgeHub.Instance.IsOpen;
        if (!bridgeOk)
        {
            _hint = "桥未连接：请先运行  .\\venv\\Scripts\\python.exe -m src.bridge";
            Debug.LogWarning("[PlayerChat] 桥未连接，消息未发出");
            return;
        }

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
            _hint = "没找到小念（NpcBridgeClient/Agent 都不在场景里）。";
            Debug.LogWarning("[PlayerChat] 无法发送：无可用通道");
            return;
        }

        _hint = $"→ {receiverName}: {t}";
        _text = "";
        CloseChat();

        // 「过来/到我面前」类指令：小念移动到玩家身前 1 米（本地导航，桥流程不受影响）
        if (t.Contains("过来") || t.Contains("到我面前") || t.Contains("到我这里") ||
            t.Contains("到我身边") || t.Contains("靠近我") || t.Contains("来我这里") ||
            t.Contains("近一点") || t.Contains("走近"))
        {
            var agent = FindObjectOfType<NpcAgent>();
            var ac = agent != null ? agent.GetComponent<AgentController>() : null;
            if (ac != null)
            {
                Vector3 front = transform.position + transform.forward * 1.0f;
                ac.HandleCommand("move", front, null);
            }
        }
    }

    void CloseChat()
    {
        _showInput = false;
        LockCursor();
    }

    static void ReleaseCursor()
    {
        ChatUiOpen = true;
        Cursor.lockState = CursorLockMode.None;
        Cursor.visible = true;
    }

    static void LockCursor()
    {
        ChatUiOpen = false;
        Cursor.lockState = CursorLockMode.Locked;
        Cursor.visible = false;
    }

    void ResolveTarget()
    {
        if (targetNpc != null || targetAgent != null) return;

        var clients = FindObjectsOfType<NpcBridgeClient>();
        var agents = FindObjectsOfType<NpcAgent>();

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
