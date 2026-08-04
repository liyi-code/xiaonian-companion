// ActionContextProvider.cs
// 示例：把 Unity 里的环境状态转成“概念种子”喂给小念的意识层，并执行她返回的动作意图。
// 挂在每个 NPC（或一个全局管理器）上，把 NpcBridgeClient 拖进来。
//
// 用法：
// 1. 给场景里的可交互物体加上 Tag（如 Chair、Player、Tree、Enemy）。
// 2. 在 NPC 的 Animator 里创建 Trigger：ACT_IDLE / ACT_SIT / ACT_WAVE / ACT_LOOKAROUND 等。
// 3. 把本脚本挂到 NPC 上，拖入 NpcBridgeClient 和 Animator。
// 4. 运行后，NPC 每隔几秒会从 Python 拿到一个 [ACT_xxx] 意图并触发对应动画。

using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Events;

[RequireComponent(typeof(NpcBridgeClient))]
public class ActionContextProvider : MonoBehaviour
{
    [Header("引用")]
    public NpcBridgeClient bridge;           // 自动获取同物体上的 NpcBridgeClient
    public Animator animator;                // 要驱动的 Animator
    public Transform player;                 // 玩家（为空则自动找 MainCamera/Player）

    [Header("环境感知")]
    [Tooltip("检测半径内的物体作为环境上下文")]
    public float detectRadius = 8f;
    [Tooltip("只检测这些 Tag 的物体；留空则检测所有")]
    public List<string> detectTags = new List<string> { "Chair", "Player", "Tree", "Enemy" };
    [Tooltip("多少时间判定一次动作执行成功")]
    public float actionDuration = 2f;

    [Header("事件")]
    [Tooltip("收到动作意图时触发，参数为 [ACT_xxx]")]
    public UnityEvent<string> onActionIntent;

    // 当前正在执行的动作（用于反馈）
    private string _currentAction;
    private float _actionTimer;
    private bool _actionPending;

    void Start()
    {
        if (bridge == null) bridge = GetComponent<NpcBridgeClient>();
        if (animator == null) animator = GetComponentInChildren<Animator>();
        if (player == null)
        {
            var go = GameObject.FindGameObjectWithTag("Player");
            if (go != null) player = go.transform;
            if (player == null && Camera.main != null) player = Camera.main.transform;
        }
        if (bridge != null)
            bridge.onActionIntent.AddListener(HandleActionIntent);
    }

    void Update()
    {
        GatherAndSendContext();
        CheckActionCompletion();
    }

    /// <summary>
    /// 收集环境上下文并推给 NpcBridgeClient，供下次自发动作请求使用。
    /// 你可以在这里加入任何自定义状态：时间、天气、血量、任务进度等。
    /// </summary>
    void GatherAndSendContext()
    {
        var ctx = new List<string>();

        // 1. 时间（简单示例：按真实时间分白天/晚上）
        int hour = System.DateTime.Now.Hour;
        if (hour >= 6 && hour < 18) ctx.Add("白天");
        else if (hour >= 18 && hour < 22) ctx.Add("傍晚");
        else ctx.Add("晚上");

        // 2. 玩家距离
        if (player != null)
        {
            float d = Vector3.Distance(transform.position, player.position);
            if (d < 3f) ctx.Add("玩家很近");
            else if (d < 8f) ctx.Add("玩家附近");
            else ctx.Add("玩家较远");
        }

        // 3. 附近物体（按 Tag）
        Collider[] hits = Physics.OverlapSphere(transform.position, detectRadius);
        foreach (var c in hits)
        {
            if (c.gameObject == gameObject) continue;
            string tag = c.gameObject.tag;
            if (detectTags.Count == 0 || detectTags.Contains(tag))
            {
                string concept = $"附近有{tag}";
                if (!ctx.Contains(concept)) ctx.Add(concept);
            }
        }

        // 4. 自己状态
        ctx.Add("空闲");

        if (bridge != null)
            bridge.SetActionContext(ctx);
    }

    void HandleActionIntent(string action)
    {
        _currentAction = action;
        _actionPending = true;
        _actionTimer = 0f;

        // 触发 Animator 里的同名 Trigger（前提是你已经在 Animator 里建了这些 Trigger）
        if (animator != null)
        {
            string trigger = action.Trim('[', ']'); // [ACT_SIT] -> ACT_SIT
            animator.SetTrigger(trigger);
        }

        Debug.Log($"[ActionContext] {bridge?.npcId} 执行动作: {action}");
        onActionIntent?.Invoke(action);
    }

    void CheckActionCompletion()
    {
        if (!_actionPending) return;
        _actionTimer += Time.deltaTime;
        if (_actionTimer >= actionDuration)
        {
            // 示例：默认认为成功；你可以在这里做射线检测/寻路结果判定，真的失败时传 false
            bool success = true;
            if (bridge != null && !string.IsNullOrEmpty(_currentAction))
                bridge.ReportActionFeedback(success);
            _actionPending = false;
        }
    }

    void OnDrawGizmosSelected()
    {
        Gizmos.color = Color.cyan;
        Gizmos.DrawWireSphere(transform.position, detectRadius);
    }
}
