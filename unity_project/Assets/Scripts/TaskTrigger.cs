// TaskTrigger.cs
// 可交互物体上的「任务触发/进度上报」组件。挂在场景物体上，玩家进入范围或点击时，
// 向对应 NPC 上报一个 quest_event（reach/interact/talk），由 Python 端推进任务进度。
//
// 用法：在小念能交互的物体（宝箱/石碑/NPC 碰撞体）上挂本脚本，填 targetNpcId 与 objectId，
//       objectId 必须与 Python quest.py 任务模板里的 objective.target 一致。
using UnityEngine;

public class TaskTrigger : MonoBehaviour
{
    [Header("任务绑定")]
    public string targetNpcId = "default";   // 归属哪个 NPC 的任务
    public string objectId;                  // 与任务 objective.target 对应
    public QuestKind kind = QuestKind.interact;

    [Header("交互方式")]
    public bool triggerOnEnter = true;       // 进入碰撞体即触发
    public bool triggerOnClick = true;       // 点击触发（需 Collider + 本物体可 Raycast）

    public enum QuestKind { reach, interact, talk, custom }

    private bool _fired;

    void OnTriggerEnter(Collider other)
    {
        if (triggerOnEnter && !_fired && IsPlayer(other))
        {
            _fired = true;
            Fire();
        }
    }

    void OnMouseDown()
    {
        if (triggerOnClick) Fire();
    }

    // 也可被其它脚本（如交互键）显式调用
    public void Fire()
    {
        BridgeHub.Instance?.SendQuestEvent(targetNpcId, kind.ToString(), objectId);
        Debug.Log($"[TaskTrigger] {objectId} -> {targetNpcId} ({kind})");
    }

    private static bool IsPlayer(Collider c)
    {
        // 约定：玩家物体 tag == "Player"
        return c.CompareTag("Player");
    }
}
