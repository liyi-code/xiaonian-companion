// FurnitureMeasure.cs —— 测量 Kenney 家具模型的真实尺寸（编辑器工具）
// ============================================================================
// 菜单 Tools > VRHome > 导出家具测量：遍历家具包全部 FBX，实例化到临时位置，
// 用 Renderer.bounds 计算每个模型的真实尺寸（Unity 空间，Y 轴朝上）与
// 模型底部相对枢轴的偏移（bottomY），写入工程根目录 furniture_measure.txt。
// 用这份数据反推正确的 scale 与 y 落位。
// ============================================================================
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEngine;

public static class FurnitureMeasure
{
    const string FBX_DIR = "Assets/ImportedAssets/Kenney_FurnitureKit/Models/FBX format/";

    [MenuItem("Tools/VRHome/导出家具测量")]
    public static void Export()
    {
        var guids = AssetDatabase.FindAssets("t:Model", new[] { FBX_DIR });
        var sb = new System.Text.StringBuilder();
        int n = 0;
        foreach (var g in guids)
        {
            string path = AssetDatabase.GUIDToAssetPath(g);
            if (!path.EndsWith(".fbx")) continue;
            var asset = AssetDatabase.LoadAssetAtPath<GameObject>(path);
            if (asset == null) continue;
            var go = Object.Instantiate(asset);
            go.transform.position = Vector3.zero;
            go.transform.rotation = Quaternion.identity;
            go.transform.localScale = Vector3.one;
            var rs = go.GetComponentsInChildren<Renderer>();
            if (rs.Length == 0) { Object.DestroyImmediate(go); continue; }
            Bounds b = rs[0].bounds;
            foreach (var r in rs) b.Encapsulate(r.bounds);
            float bottomY = b.min.y;   // go 在原点 → 底部相对枢轴
            sb.AppendLine($"{Path.GetFileNameWithoutExtension(path)}|{b.size.x:F3}|{b.size.y:F3}|{b.size.z:F3}|{bottomY:F3}|{rs.Length}");
            Object.DestroyImmediate(go);
            n++;
        }
        string outPath = Path.Combine(Directory.GetParent(Application.dataPath).FullName, "furniture_measure.txt");
        File.WriteAllText(outPath, sb.ToString());
        Debug.Log($"[Measure] 已测量 {n} 个模型 → {outPath}");
    }
}
