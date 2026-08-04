// NpcManager.cs
// 管理场景里所有 NPC 的人形实例（VRM 预制体），并按 BridgeHub 的 ready/spawn/despawn 事件
// 创建/销毁对应的 NpcAgent。每个 NPC 对应一个小念 VRM 克隆 + 一个 NpcAgent 行为脚本。
//
// 用法：
//   1) 把你的「小念 VRM 转成的 prefab」拖进 Inspector 的 npcPrefab 槽（或运行时 Resources.Load）。
//   2) 场景里放一个空 GameObject 挂本脚本（也可和 BridgeHub 同物体）。
//   3) BridgeHub 连上后，会回调 OnReady(列表) —— 这里为每个 NPC 生成实例。
using System.Collections.Generic;
using UnityEngine;

public class NpcManager : MonoBehaviour
{
    [Header("NPC 预制体（小念 VRM 转成的 prefab）")]
    public GameObject npcPrefab;          // 含 Animator + VRM 面部 BlendShapeProxy
    public float spawnSpacing = 1.5f;     // 多个 NPC 生成时的横向间距

    private readonly Dictionary<string, GameObject> _instances = new Dictionary<string, GameObject>();
    private int _spawnIndex = 0;

    void Awake()
    {
        if (BridgeHub.Instance != null)
            BridgeHub.Instance.OnReady += OnReady;
    }

    // Python 告知当前应存在的 NPC 列表（含 default）
    public void OnReady(List<NpcInfo> list)
    {
        foreach (var info in list)
            SpawnAgent(info.npcId, info.name);
    }

    public void SpawnAgent(string npcId, string name)
    {
        if (_instances.ContainsKey(npcId)) return;   // 已存在
        if (npcPrefab == null)
        {
            Debug.LogError("[NpcManager] npcPrefab 未配置，无法生成 " + npcId);
            return;
        }
        var go = Instantiate(npcPrefab, transform);
        // 横向排开，避免重叠
        go.transform.position = new Vector3(_spawnIndex * spawnSpacing, 0, 0);
        _spawnIndex++;

        var agent = go.AddComponent<NpcAgent>();
        agent.Init(npcId, name, go);
        _instances[npcId] = go;
        Debug.Log($"[NpcManager] 生成 NPC: {name} ({npcId})");
    }

    public void DespawnAgent(string npcId)
    {
        if (_instances.TryGetValue(npcId, out var go))
        {
            _instances.Remove(npcId);
            Destroy(go);
            Debug.Log($"[NpcManager] 销毁 NPC: {npcId}");
        }
    }
}
