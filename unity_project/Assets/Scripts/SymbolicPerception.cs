/*
 * SymbolicPerception —— 小念在 3D 世界里的「符号感知」采集器（无图像）。
 *
 * 多 NPC 版本：每个 NPC 实例（挂了 NpcAgent 的 VRM）上挂一个本脚本，感知数据按
 * 该 NPC 的 npcId 推给 Python（每个 NPC 有独立的 world_state，互不串台）。
 *
 * 它做三件事，全部通过 BridgeHub 推给 Python 端（src/bridge.py + world_state.py）：
 *   1) 区域(预加载)上报：当前场景名作为 region，加载时 world_load(loaded=true)。
 *   2) 符号感知：每 perceptInterval 秒，遍历自身 radius 米内的可感知物体（挂了
 *      PerceptTag 的物体），组装成 symbolic_percept 推给小念。
 *   3) 低频视觉快照：每 snapshotInterval 秒，用挂在自身的 Camera 截一张图，
 *      编码 base64 推 visual_snapshot，由 Python 走视觉 API「结合符号感知」推理。
 *
 * 接入步骤：
 *   - 给每个小念 VRM 预制体挂本脚本（与 NpcAgent 同物体）。
 *   - 给场景里“小念能感知”的物体挂 PerceptTag.cs，填好 id/name/type。
 *   - （可选）挂一个 Camera（小念视角），不挂则跳过视觉快照。
 */

using System.Collections.Generic;
using UnityEngine;

public class SymbolicPerception : MonoBehaviour
{
    [Header("符号感知")]
    public float perceptInterval = 0.5f;     // 符号感知频率（秒）
    public float radius = 14f;               // 小念能“看到”的半径(米)
    public LayerMask perceptLayer = -1;

    [Header("视觉快照(低频)")]
    public Camera watchCamera;               // 小念视角相机（不挂则禁用视觉）
    public float snapshotInterval = 15f;
    public int snapWidth = 1920;
    public int snapHeight = 1080;
    public int snapQuality = 82;

    private NpcAgent _agent;
    private float nextPercept = 0f;
    private float nextSnap = 0f;
    private string _region = "";
    private bool _worldLoadSent = false;
    private bool _prevOpen = false;

    void Awake()
    {
        _agent = GetComponent<NpcAgent>();
        if (_agent == null) _agent = FindObjectOfType<NpcAgent>();
        _region = UnityEngine.SceneManagement.SceneManager.GetActiveScene().name;
        // 注意：不能在 Awake 直接 SendWorldLoad——此时 WS 通常还没连上，
        // BridgeHub.Send 会静默丢弃，Python 端 loaded_regions 永远为空。
        // 改为在 Update 里检测到连接建立后补发（见下）。
    }

    void OnDestroy()
    {
        var region = UnityEngine.SceneManagement.SceneManager.GetActiveScene().name;
        BridgeHub.Instance?.SendWorldLoad(NpcId, region, false);
    }

    private string NpcId => _agent != null ? _agent.npcId : "default";

    void Update()
    {
        if (BridgeHub.Instance == null) { _prevOpen = false; return; }

        // 每次连接建立（含断线重连）都补发一次 world_load：
        // 否则 Python 端 loaded_regions 为空 → world_state 丢弃所有 symbolic_percept →
        // explorer 的 has_loaded() 永远 False → NPC 不会自己走动探索。
        bool open = BridgeHub.Instance.IsOpen;
        if (open && !_prevOpen) _worldLoadSent = false;   // 新连接 → 重置标记
        if (open && !_worldLoadSent)
        {
            _worldLoadSent = true;
            BridgeHub.Instance.SendWorldLoad(NpcId, _region, true);
        }
        _prevOpen = open;

        if (Time.time >= nextPercept)
        {
            nextPercept = Time.time + perceptInterval;
            PushPercepts();
        }
        if (watchCamera != null && Time.time >= nextSnap)
        {
            nextSnap = Time.time + snapshotInterval;
            PushSnapshot();
        }
    }

    public void ReportRegion(string regionId, bool loaded)
    {
        BridgeHub.Instance?.SendWorldLoad(NpcId, regionId, loaded);
    }

    private void PushPercepts()
    {
        Vector3 center = transform.position;
        Collider[] hits = Physics.OverlapSphere(center, radius, perceptLayer);
        var list = new List<PerceptObject>();
        var dedup = new HashSet<PerceptTag>();
        foreach (var c in hits)
        {
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
        var payload = new Newtonsoft.Json.Linq.JObject();
        payload["agent_pos"] = JVec(center);
        var arr = new Newtonsoft.Json.Linq.JArray();
        foreach (var o in list)
        {
            var jo = new Newtonsoft.Json.Linq.JObject
            {
                ["id"] = o.id, ["name"] = o.name, ["type"] = o.type,
                ["pos"] = JVec(o.pos), ["state"] = o.state, ["region"] = o.region,
            };
            arr.Add(jo);
        }
        payload["objects"] = arr;
        BridgeHub.Instance.SendSymbolicPercept(NpcId, payload);
    }

    private static Newtonsoft.Json.Linq.JObject JVec(Vector3 v)
    {
        return new Newtonsoft.Json.Linq.JObject { ["x"] = v.x, ["y"] = v.y, ["z"] = v.z };
    }

    private void PushSnapshot()
    {
        if (watchCamera == null) return;
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
        BridgeHub.Instance.SendVisualSnapshot(NpcId, JVec(watchCamera.transform.position).ToString(), b64);
    }
}
