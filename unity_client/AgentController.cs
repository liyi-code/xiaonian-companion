/*
 * AgentController —— 接收小念主动探索指令，驱动她的 3D 身体在世界里活动。
 *
 * 它与 XiaonianBridge 挂在同一 GameObject（小念的 VRM 模型）上。
 * Python 端的 AutonomousExplorer 经 WebSocket 下发 agent_command，这里负责执行：
 *   - move     : 走向目标坐标（按 world_move_speed 平滑移动）
 *   - look     : 转身面向目标（可借此抓一帧视觉快照）
 *   - interact : 走近后触发交互动作（这里用 Animator 触发器 + Debug；按项目替换）
 *   - wander   : 没有有意思目标时随意踱步，制造“她在自己活动”的感觉
 *
 * 注意：这一切都在«玩家不交互»时发生——小念是“主动”探索，不是等玩家命令。
 * 移动基于 Transform；若你的项目用 NavMesh / 物理角色控制器，把 MoveTo 内部替换即可。
 */

using UnityEngine;

[RequireComponent(typeof(XiaonianBridge))]
public class AgentController : MonoBehaviour
{
    [Header("移动")]
    public float moveSpeed = 2.5f;        // 米/秒（与 Python 端 world_move_speed 对应）
    public float arriveRadius = 0.4f;     // 到达判定半径
    public float turnSpeed = 4f;          // 转身速度

    [Header("交互动作")]
    public string interactTrigger = "interact";   // Animator 触发器名（需在动画状态机建好）

    private Vector3? moveTarget = null;
    private bool faceTarget = false;
    private Vector3? lookAt = null;

    private XiaonianBridge bridge;

    void Awake()
    {
        bridge = GetComponent<XiaonianBridge>();
    }

    // 由 XiaonianBridge.HandleEvent("agent_command") 调用
    public void HandleCommand(string action, Vec3 target, string objectId)
    {
        if (string.IsNullOrEmpty(action)) return;
        switch (action)
        {
            case "move":
                if (target != null)
                {
                    moveTarget = target.ToVector3();
                    faceTarget = false;   // 移动时朝移动方向转身，取消“看”的抢转
                }
                break;
            case "look":
                if (target != null) { lookAt = target.ToVector3(); faceTarget = true; }
                break;
            case "interact":
                DoInteract(objectId);
                break;
            case "wander":
                // 在自身周围随机选一点踱步
                Vector3 r = transform.position
                    + new Vector3(Random.Range(-4f, 4f), 0f, Random.Range(-4f, 4f));
                moveTarget = r;
                break;
        }
    }

    void Update()
    {
        // 平滑移动
        if (moveTarget.HasValue)
        {
            Vector3 dir = moveTarget.Value - transform.position;
            dir.y = 0f;
            float dist = dir.magnitude;
            if (dist <= arriveRadius)
            {
                moveTarget = null;
                StopMoving();
            }
            else
            {
                Vector3 step = dir.normalized * Mathf.Min(moveSpeed * Time.deltaTime, dist);
                transform.position += step;
                // 朝移动方向转身
                Quaternion to = Quaternion.LookRotation(dir.normalized);
                transform.rotation = Quaternion.Slerp(transform.rotation, to, turnSpeed * Time.deltaTime);
                PlayMoveAnim(true);
            }
        }

        // 仅“看”的转身（不动位置；移动中不生效，避免与移动转身冲突）
        if (faceTarget && lookAt.HasValue && !moveTarget.HasValue)
        {
            Vector3 d = lookAt.Value - transform.position;
            d.y = 0f;
            if (d.sqrMagnitude > 0.001f)
            {
                Quaternion to = Quaternion.LookRotation(d.normalized);
                transform.rotation = Quaternion.Slerp(transform.rotation, to, turnSpeed * Time.deltaTime);
                // 已基本朝向目标 → 完成“看”，复位（否则永远抢占转身）
                if (Quaternion.Angle(transform.rotation, to) < 3f) faceTarget = false;
            }
            else faceTarget = false;
        }
    }

    private void DoInteract(string objectId)
    {
        Debug.Log("[小念][交互] 与物体交互：" + (objectId ?? "?"));
        var anim = GetComponent<Animator>();
        if (anim != null && !string.IsNullOrEmpty(interactTrigger))
            anim.SetTrigger(interactTrigger);
        // 若需要把交互结果回报给小念大脑，可在这里调用 bridge.SendUserInput(...) 或自定义事件
    }

    // —— 移动动画：用 Animator 的 "isMoving" 布尔（需在动画状态机建好；缺省静默）——
    private bool wasMoving = false;
    private void PlayMoveAnim(bool moving)
    {
        if (moving == wasMoving) return;
        wasMoving = moving;
        var anim = GetComponent<Animator>();
        if (anim != null) anim.SetBool("isMoving", moving);
    }
    private void StopMoving() { PlayMoveAnim(false); }
}
