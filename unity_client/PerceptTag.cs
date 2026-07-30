/*
 * PerceptTag —— 挂在场景物体上，声明“小念可以符号感知到我”。
 *
 * 给任何你想让小念能“看见并主动探索”的物体挂这个脚本，填好字段即可：
 *   id    : 稳定唯一 id（同一物体每次运行保持一致，供新颖度/访问去重）
 *   name  : 显示名（推给小念的文字）
 *   type  : 类型，决定她的兴趣与是否可交互——
 *           高兴趣：treasure/chest/npc/door/lever/machine/artifact/shrine/quest/book…
 *           可交互：chest/npc/door/lever/switch/machine/book…（走到近前会触发 interact）
 *   state : 可选状态文本（open/closed/lit/…），会一并推给小念
 *
 * 仅作为“被感知”的标注；不依赖任何渲染/图像。
 */

using UnityEngine;

public class PerceptTag : MonoBehaviour
{
    public string id;
    public string displayName;
    public string type = "object";
    public string state = "";

    void Awake()
    {
        // 没填 id 就用物体名兜底（注意：同名物体会被当成同一个，建议显式填唯一 id）
        if (string.IsNullOrEmpty(id)) id = gameObject.name;
        if (string.IsNullOrEmpty(displayName)) displayName = gameObject.name;
    }
}
