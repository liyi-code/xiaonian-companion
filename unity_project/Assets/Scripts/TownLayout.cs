// TownLayout.cs
// 小镇建筑坐标静态表，与 Python 端 src/town.py 的 BUILDINGS 对齐。
// NpcAgent 接到 town_task 后，用本表把「目标建筑 id」转成世界坐标去寻路。
using System.Collections.Generic;
using UnityEngine;

public static class TownLayout
{
    // 建筑 id -> 世界坐标（x, z 与 Python 一致，y 取地面 0）
    private static readonly Dictionary<string, Vector3> _pos = new Dictionary<string, Vector3>
    {
        { "farm_field",   new Vector3(-8, 0,  6) },
        { "lumber_mill",  new Vector3( 8, 0,  6) },
        { "forest",       new Vector3( 8, 0,  6) },   // 伐木场即森林入口
        { "mine_shaft",   new Vector3(-8, 0, -6) },
        { "mine",         new Vector3(-8, 0, -6) },
        { "kitchen_stove",new Vector3( 0, 0,  0) },
        { "kitchen",      new Vector3( 0, 0,  0) },
        { "market_forge", new Vector3( 8, 0, -6) },
        { "market",       new Vector3( 8, 0, -6) },
        { "forge_furnace",new Vector3( 0, 0,  8) },
        { "forge",        new Vector3( 0, 0,  8) },
        { "well",         new Vector3( 0, 0, -8) },
    };

    public static Vector3? PosOf(string id)
    {
        return _pos.TryGetValue(id, out var v) ? (Vector3?)v : null;
    }
}
