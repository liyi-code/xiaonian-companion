// NpcBodyCollider.cs
// 给 NPC 角色根物体自动添加“身体碰撞体”，使其具有实体碰撞：
//  - CapsuleCollider：包裹整个人形，可被其他碰撞器/射线检测、能挡住/被触发
//  - Rigidbody(isKinematic)：因为角色位移由脚本直接驱动(transform)，用 kinematic
//    刚体既不会受重力乱飞，又能参与碰撞检测与 Trigger 事件，且不和物理引擎抢控制权
// 胶囊尺寸在 Start 时按角色包围盒(auto)或身高推算，无需手动在编辑器配置。
//
// 用法：把这个脚本挂到和 NpcBridgeClient / ConceptStateMachine 同一个根物体上即可，
// 也可用 [RequireComponent] 让 NpcBridgeClient 自动带上。

using UnityEngine;

[RequireComponent(typeof(CapsuleCollider))]
[RequireComponent(typeof(Rigidbody))]
public class NpcBodyCollider : MonoBehaviour
{
    [Header("碰撞体配置")]
    [Tooltip("胶囊半径（米）。0=按包围盒自动推算")]
    public float radius = 0f;
    [Tooltip("胶囊总高（米）。0=按包围盒自动推算")]
    public float height = 0f;
    [Tooltip("是否与场景动态物体物理互动（开启后 kinematic，不被动受力，但仍可触发/阻挡）")]
    public bool kinematic = true;
    [Tooltip("作为触发器（只检测不阻挡）")]
    public bool isTrigger = false;

    void Start()
    {
        Bounds b = GetRendererBounds();
        // 自动推算尺寸（若未手动指定）：按角色渲染包围盒
        if (radius <= 0f || height <= 0f)
        {
            if (b.size.y > 0.01f)
            {
                if (height <= 0f) height = b.size.y;
                if (radius <= 0f) radius = Mathf.Max(0.12f, Mathf.Min(b.size.x, b.size.z) * 0.5f * 0.6f);
            }
            else
            {
                if (height <= 0f) height = 1.6f;
                if (radius <= 0f) radius = 0.22f;
            }
        }

        var col = GetComponent<CapsuleCollider>();
        col.isTrigger = isTrigger;
        col.radius = radius;
        col.height = height;
        // 关键：胶囊中心必须放在模型包围盒中心（本地坐标）——之前写死 (0,0,0)，
        // 模型脚底在原点时胶囊只包住下半身，玩家能穿过她胸口/头。
        col.center = transform.InverseTransformPoint(b.center);

        var rb = GetComponent<Rigidbody>();
        rb.isKinematic = kinematic;
        rb.useGravity = false;
        rb.constraints = RigidbodyConstraints.FreezeAll;
        rb.collisionDetectionMode = CollisionDetectionMode.Continuous;
    }

    // 计算角色所有渲染器的合并包围盒（世界尺寸转本地）
    private Bounds GetRendererBounds()
    {
        var renderers = GetComponentsInChildren<Renderer>();
        if (renderers.Length == 0)
            return new Bounds(transform.position, Vector3.zero);

        Bounds b = renderers[0].bounds;
        foreach (var r in renderers)
            b.Encapsulate(r.bounds);
        return b;
    }
}
