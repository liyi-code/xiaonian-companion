/*
 * SymbolicPerception —— 小念在 3D 世界里的「符号感知」采集器（无图像）。
 *
 * 它做三件事，全部通过 XiaonianBridge 推给 Python 端（src/bridge.py + world_state.py）：
 *   1) 区域(预加载)上报：当前场景名作为 region，加载时 world_load(loaded=true)，
 *      场景卸载时 world_load(loaded=false)。—— 对应“走到哪、加载到哪”。
 *   2) 符号感知：每 perceptInterval 秒，遍历自身 radius 米内的可感知物体（挂了
 *      PerceptTag 的物体），组装成 symbolic_percept 推给小念。只含已加载范围内、
 *      且距离内的物体；超出范围的由 Python 端 world_state 再过滤一次。
 *   3) 低频视觉快照：每 snapshotInterval 秒，用挂在自身的 Camera 截一张 1080p 图，
 *      编码 base64 推 visual_snapshot，由 Python 走视觉 API「结合符号感知」推理。
 *
 * 接入步骤：
 *   - 把本脚本挂在一个管理器 GameObject 上（与 XiaonianBridge 不同物体也行，会自寻）。
 *   - 给场景里“小念能感知”的物体挂 PerceptTag.cs，填好 id/name/type。
 *   - （可选）给本脚本挂一个 Camera（小念第一/三人称视角），不挂则跳过视觉快照。
 *   - 预加载：把你的场景流式加载/卸载接到 ReportRegion(regionId, loaded) 即可。
 *
 * 完全不涉及任何像素进入小念大脑——符号感知就是结构化文本 + 坐标。
 */

using System.Collections.Generic;
using UnityEngine;

// 注意：不能 RequireComponent(XiaonianBridge)——本脚本允许挂在独立管理器物体上，
// Awake 里会自动 FindObjectOfType 找场景中的桥；强制依赖会凭空生成第二个桥连接。
public class SymbolicPerception : MonoBehaviour
{
    [Header("符号感知")]
    public float perceptInterval = 0.5f;     // 符号感知频率（秒）；高频、极轻量(无图)
    public float radius = 14f;               // 小念能“看到”的半径(米)
    public LayerMask perceptLayer = -1;      // 只检测指定层（可选）

    [Header("视觉快照(低频)")]
    public Camera watchCamera;               // 小念视角相机（不挂则禁用视觉）
    public float snapshotInterval = 15f;     // 视觉快照频率（秒）；低频、较重(API)
    public int snapWidth = 1920;             // 1080p
    public int snapHeight = 1080;
    public int snapQuality = 82;             // JPEG 压缩质量

    private XiaonianBridge bridge;
    private float nextPercept = 0f;
    private float nextSnap = 0f;

    void Awake()
    {
        bridge = GetComponent<XiaonianBridge>();
        if (bridge == null) bridge = FindObjectOfType<XiaonianBridge>();
        // 以当前场景名作为初始已加载区域
        ReportRegion(UnityEngine.SceneManagement.SceneManager.GetActiveScene().name, true);
    }

    void OnDestroy()
    {
        if (bridge != null)
            bridge.SendWorldLoad(UnityEngine.SceneManagement.SceneManager.GetActiveScene().name, false);
    }

    void Update()
    {
        if (bridge == null) return;

        // 1) 符号感知（高频、无图）
        if (Time.time >= nextPercept)
        {
            nextPercept = Time.time + perceptInterval;
            PushPercepts();
        }

        // 2) 低频视觉快照
        if (watchCamera != null && Time.time >= nextSnap)
        {
            nextSnap = Time.time + snapshotInterval;
            PushSnapshot();
        }
    }

    // 上报一个区域的加载/卸载状态（接你自己的场景流式加载系统时调用这个）
    public void ReportRegion(string regionId, bool loaded)
    {
        if (bridge != null) bridge.SendWorldLoad(regionId, loaded);
    }

    private void PushPercepts()
    {
        // 以自身位置为中心，球内收集带 PerceptTag 的物体
        Vector3 center = transform.position;
        Collider[] hits = Physics.OverlapSphere(center, radius, perceptLayer);
        var list = new List<PerceptObject>();
        var dedup = new HashSet<PerceptTag>();   // 复合碰撞体(多 collider 同物体)去重
        foreach (var c in hits)
        {
            // collider 常挂在子物体上，用 GetComponentInParent 向上找标注
            var tag = c.GetComponentInParent<PerceptTag>();
            if (tag == null || !dedup.Add(tag)) continue;
            list.Add(new PerceptObject
            {
                id = tag.id,
                name = tag.displayName,
                type = tag.type,
                pos = tag.transform.position,
                state = tag.state,
                region = UnityEngine.SceneManagement.SceneManager.GetActiveScene().name,
            });
        }
        bridge.SendSymbolicPercept(center, list);
    }

    private void PushSnapshot()
    {
        if (watchCamera == null) return;
        // 截 1080p RenderTexture -> 读回 -> 编码 JPEG base64
        var rt = RenderTexture.GetTemporary(snapWidth, snapHeight, 24, RenderTextureFormat.ARGB32);
        var prev = watchCamera.targetTexture;
        watchCamera.targetTexture = rt;
        watchCamera.Render();
        RenderTexture.active = rt;

        var tex = new Texture2D(snapWidth, snapHeight, TextureFormat.RGB24, false);
        tex.ReadPixels(new Rect(0, 0, snapWidth, snapHeight), 0, 0);
        tex.Apply();

        watchCamera.targetTexture = prev;
        RenderTexture.active = null;
        RenderTexture.ReleaseTemporary(rt);

        byte[] jpg = tex.EncodeToJPG(snapQuality);
        Object.Destroy(tex);
        if (jpg == null) return;
        string b64 = System.Convert.ToBase64String(jpg);
        bridge.SendVisualSnapshot(b64);
    }
}
