// TownView.cs
// 村庄面板：接收 Python 小镇（town_state / town_task）全局广播，把「我的世界村庄」
// 式自给自足小镇的资源/村民/建筑/自给自足状态渲染到屏幕 UI。
// 同时把建筑位置画成小图标，短缺任务目标建筑高亮闪烁。
//
// 挂法：场景里放一个空 GameObject，挂本脚本；Inspector 可选 townPanel 容器。
// 前置：BridgeHub.cs（提供 OnTownState / OnTownTask 事件）。
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.UI;
using Newtonsoft.Json.Linq;

public class TownView : MonoBehaviour
{
    [Header("UI")]
    public GameObject panelRoot;     // 村庄面板根（Canvas 下的容器）
    public Text dayText;
    public Text selfSufficientText;
    public Text resourceText;
    public Text villagerText;
    public Text taskText;

    [Header("建筑标记")]
    public GameObject buildingMarkerPrefab;   // 小方块 prefab，代表一栋建筑
    public float markerScale = 1f;

    private readonly Dictionary<string, GameObject> _markers = new Dictionary<string, GameObject>();
    private readonly List<string> _activeTaskTargets = new List<string>();
    private float _blink;

    void Start()
    {
        if (BridgeHub.Instance != null)
        {
            BridgeHub.Instance.OnTownState += OnState;
            BridgeHub.Instance.OnTownTask += OnTask;
        }
        else
        {
            Debug.LogWarning("[TownView] 未找到 BridgeHub 实例，村庄面板不会更新");
        }
    }

    void Update()
    {
        // 短缺建筑标记闪烁
        _blink += Time.deltaTime * 3f;
        float a = 0.4f + 0.6f * (0.5f + 0.5f * Mathf.Sin(_blink));
        foreach (var kv in _markers)
        {
            var rend = kv.Value.GetComponent<Renderer>();
            if (rend != null)
            {
                bool isTarget = _activeTaskTargets.Contains(kv.Key);
                rend.material.color = isTarget
                    ? new Color(1f, 0.3f, 0.3f, a)   // 短缺 → 红闪
                    : new Color(0.6f, 0.8f, 1f, 0.8f); // 正常 → 蓝
            }
        }
    }

    // ---------------- 接收 Python 广播 ----------------
    void OnState(JObject ev)
    {
        int day = (int?)ev["day"] ?? 1;
        bool ss = (bool?)ev["self_sufficient"] ?? false;

        if (dayText) dayText.text = $"第 {day} 天";
        if (selfSufficientText)
        {
            selfSufficientText.text = ss ? "✅ 自给自足" : "⚠ 资源短缺中";
            selfSufficientText.color = ss ? Color.green : Color.red;
        }

        if (resourceText)
        {
            var sb = new System.Text.StringBuilder();
            foreach (var r in ((JObject)(ev["resources"] ?? new JObject())).Properties())
                sb.AppendLine($"{r.Name}: {r.Value}");
            resourceText.text = sb.ToString();
        }

        if (villagerText)
        {
            var sb = new System.Text.StringBuilder();
            foreach (var v in ev["villagers"] ?? new JArray())
            {
                string nm = (string)v["name"];
                string role = (string)v["role_name"];
                sb.AppendLine($"· {nm}（{role}）");
            }
            villagerText.text = sb.ToString();
        }

        // 建筑标记（只建一次，之后保持）
        foreach (var b in ev["buildings"] ?? new JArray())
        {
            string id = (string)b["id"];
            if (_markers.ContainsKey(id)) continue;
            var pos = b["pos"];
            float x = (float?)pos["x"] ?? 0;
            float z = (float?)pos["z"] ?? 0;
            SpawnMarker(id, x, z);
        }
    }

    void OnTask(JObject ev)
    {
        string target = (string)ev["objective"]?["target"];
        string title = (string)ev["title"];
        if (!string.IsNullOrEmpty(target)) _activeTaskTargets.Add(target);
        if (taskText)
            taskText.text = $"⚠ 任务: {title}\n目标: {target}\n奖励: {(string)ev["reward"]}";
        Debug.Log($"[TownView] 接到小镇任务: {title} -> {target}");
    }

    void SpawnMarker(string id, float x, float z)
    {
        if (buildingMarkerPrefab == null) return;
        var go = Instantiate(buildingMarkerPrefab,
                             new Vector3(x, 0.5f, z), Quaternion.identity);
        go.name = "Marker_" + id;
        go.transform.localScale *= markerScale;
        _markers[id] = go;
    }

    // 玩家/村民上缴资源（上行到 Python）
    public void Contribute(string npcId, string resource, float amount)
    {
        BridgeHub.Instance?.Send(npcId, "town_contribute", o =>
        {
            o["resource"] = resource;
            o["amount"] = amount;
        });
    }

    // 村民到达/交互建筑 → 完成一轮生产（上行到 Python）
    public void ReportTownEvent(string npcId, string objectId)
    {
        BridgeHub.Instance?.Send(npcId, "town_event", o =>
        {
            o["kind"] = "interact";
            o["object_id"] = objectId;
        });
    }
}
