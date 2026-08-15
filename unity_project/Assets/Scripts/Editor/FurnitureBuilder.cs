// FurnitureBuilder.cs —— VR 家的家具摆放器（编辑器工具，运行时实测版）
// ============================================================================
// 菜单 Tools > VRHome > 摆放家具（全部）：
//   每件家具：实例化 → 实测 Renderer 包围盒（真实导入尺寸）→
//   按目标真实尺寸计算三轴缩放 → 枢轴纠偏（模型角枢轴转中心）→ 落位 + MeshCollider。
//   因此无论 FBX 以何种比例导入（cm/m 等），尺寸都自动正确。
// 菜单 Tools > VRHome > 清除全部家具：删除 FURN_ 前缀物体。
//
// LAYOUT 字段：(fbx名, 房间中心x, 房间中心z, rotY, y覆盖, 目标宽X, 目标高Y, 目标深Z)
//   y覆盖 = null → 底部贴地；数值 → 枢轴世界Y（壁灯/镜子/吊灯/桌上物品用）
// ============================================================================
using System.Collections.Generic;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

public static class FurnitureBuilder
{
    const string FBX_DIR = "Assets/ImportedAssets/Kenney_FurnitureKit/Models/FBX format/";
    const string PREFIX = "FURN_";

    struct Item
    {
        public string file;
        public float cx, cz, ry, yov, tw, th, td;
        public Item(string f, float x, float z, float r, float? y, float w, float h, float d)
        {
            file = f; cx = x; cz = z; ry = r;
            yov = y ?? float.NaN; tw = w; th = h; td = d;
        }
    }

    static readonly List<Item> LAYOUT = new List<Item>
    {
        // —— 客厅 ——
        new Item("rugRectangle", 4.0f, 9.0f, 0, null, 3.2f, 0.02f, 1.9f),
        new Item("loungeSofaLong", 2.0f, 10.8f, 180, null, 2.6f, 0.95f, 0.95f),
        new Item("loungeSofa", 5.2f, 10.9f, 180, null, 2.4f, 0.95f, 1.0f),
        new Item("tableCoffeeSquare", 3.2f, 9.4f, 0, null, 1.0f, 0.5f, 1.0f),
        new Item("cabinetTelevision", 1.5f, 6.8f, 0, null, 1.8f, 0.5f, 0.5f),
        new Item("televisionModern", 1.5f, 6.6f, 0, 0.5f, 1.5f, 1.0f, 0.3f),
        new Item("sideTableDrawers", 0.9f, 10.5f, 180, null, 0.6f, 0.55f, 0.5f),
        new Item("chairCushion", 7.2f, 10.6f, 180, null, 0.6f, 0.95f, 0.6f),
        new Item("lampRoundFloor", -3.2f, 7.5f, 0, null, 0.3f, 1.7f, 0.3f),
        new Item("pottedPlant", 11.2f, 6.9f, 0, null, 0.5f, 1.2f, 0.5f),
        new Item("coatRackStanding", 11.2f, 10.9f, 0, null, 0.5f, 1.8f, 0.5f),
        // —— 练舞厅 ——
        new Item("bathroomMirror", -3.72f, 13.6f, 90, 1.3f, 1.2f, 0.8f, 0.1f),
        new Item("bathroomMirror", -3.72f, 15.0f, 90, 1.3f, 1.2f, 0.8f, 0.1f),
        new Item("bathroomMirror", -3.72f, 16.4f, 90, 1.3f, 1.2f, 0.8f, 0.1f),
        new Item("speaker", 3.0f, 17.5f, 180, null, 0.4f, 1.0f, 0.4f),
        new Item("speaker", -1.0f, 17.5f, 180, null, 0.4f, 1.0f, 0.4f),
        new Item("rugSquare", 4.0f, 15.0f, 0, null, 2.4f, 0.02f, 2.4f),
        new Item("benchCushionLow", -2.5f, 13.0f, 0, null, 1.8f, 0.5f, 0.5f),
        new Item("lampSquareFloor", 10.8f, 13.4f, 0, null, 0.25f, 1.6f, 0.25f),
        // —— 厨房 ——
        new Item("kitchenFridgeLarge", -11.3f, 13.2f, 90, null, 0.9f, 1.9f, 0.8f),
        new Item("kitchenStove", -10.6f, 17.4f, 180, null, 0.9f, 0.9f, 0.6f),
        new Item("kitchenSink", -9.2f, 17.4f, 180, null, 0.9f, 0.9f, 0.6f),
        new Item("kitchenCabinet", -7.8f, 17.4f, 180, null, 0.9f, 0.9f, 0.6f),
        new Item("kitchenCabinetUpperDouble", -9.4f, 17.35f, 180, 2.1f, 0.9f, 0.7f, 0.35f),
        new Item("kitchenBar", -8.0f, 15.4f, 0, null, 1.6f, 1.05f, 0.6f),
        new Item("stoolBar", -8.9f, 14.3f, 0, null, 0.45f, 0.75f, 0.45f),
        new Item("stoolBar", -7.1f, 14.3f, 0, null, 0.45f, 0.75f, 0.45f),
        new Item("tableCross", -5.5f, 13.5f, 0, null, 1.3f, 0.75f, 0.8f),
        new Item("chairCushion", -6.5f, 13.5f, 90, null, 0.6f, 0.95f, 0.6f),
        new Item("chairCushion", -4.9f, 13.5f, -90, null, 0.6f, 0.95f, 0.6f),
        new Item("trashcan", -11.5f, 17.6f, 0, null, 0.4f, 0.6f, 0.4f),
        new Item("pottedPlant", -4.7f, 17.2f, 0, null, 0.5f, 1.2f, 0.5f),
        // —— 大卧室 ——
        new Item("bedDouble", -8.0f, 10.9f, 0, null, 1.7f, 0.8f, 2.1f),
        new Item("cabinetBedDrawerTable", -11.0f, 10.9f, 0, null, 0.5f, 0.55f, 0.45f),
        new Item("cabinetBedDrawerTable", -5.0f, 10.9f, 0, null, 0.5f, 0.55f, 0.45f),
        new Item("lampSquareTable", -11.0f, 10.9f, 0, 0.55f, 0.3f, 0.55f, 0.3f),
        new Item("lampSquareTable", -5.0f, 10.9f, 0, 0.55f, 0.3f, 0.55f, 0.3f),
        new Item("pillow", -8.6f, 11.6f, 0, 0.8f, 0.5f, 0.12f, 0.4f),
        new Item("pillow", -7.4f, 11.6f, 0, 0.8f, 0.5f, 0.12f, 0.4f),
        new Item("rugRound", -8.0f, 8.7f, 0, null, 2.4f, 0.02f, 2.4f),
        new Item("bookcaseClosedWide", -11.5f, 7.0f, 90, null, 1.6f, 1.8f, 0.35f),
        new Item("chairModernCushion", -6.0f, 8.5f, 0, null, 0.6f, 0.95f, 0.6f),
        new Item("pottedPlant", -4.6f, 11.4f, 0, null, 0.5f, 1.2f, 0.5f),
        // —— 书房 ——
        new Item("deskCorner", -5.5f, 3.0f, 90, null, 1.6f, 0.75f, 1.6f),
        new Item("chairDesk", -6.4f, 3.0f, -90, null, 0.5f, 0.95f, 0.55f),
        new Item("computerScreen", -5.3f, 3.0f, 90, 0.75f, 0.55f, 0.4f, 0.12f),
        new Item("computerKeyboard", -5.6f, 2.6f, 90, 0.75f, 0.45f, 0.04f, 0.15f),
        new Item("computerMouse", -5.9f, 3.3f, 0, 0.75f, 0.08f, 0.04f, 0.12f),
        new Item("bookcaseOpen", -11.4f, 1.4f, 90, null, 1.6f, 1.8f, 0.35f),
        new Item("bookcaseOpen", -11.4f, 4.6f, 90, null, 1.6f, 1.8f, 0.35f),
        new Item("tableCross", -9.0f, 1.2f, 0, null, 1.3f, 0.75f, 0.8f),
        new Item("lampSquareTable", -9.0f, 1.2f, 0, 0.75f, 0.3f, 0.55f, 0.3f),
        new Item("rugSquare", -8.0f, 2.4f, 0, null, 2.4f, 0.02f, 2.4f),
        new Item("pottedPlant", -4.6f, 0.7f, 0, null, 0.5f, 1.2f, 0.5f),
        // —— 电竞房 ——
        new Item("desk", -2.2f, 1.1f, 180, null, 1.6f, 0.75f, 0.7f),
        new Item("desk", 2.2f, 1.1f, 180, null, 1.6f, 0.75f, 0.7f),
        new Item("chairModernFrameCushion", -2.2f, 2.1f, 0, null, 0.6f, 0.95f, 0.6f),
        new Item("chairModernFrameCushion", 2.2f, 2.1f, 0, null, 0.6f, 0.95f, 0.6f),
        new Item("computerScreen", -2.2f, 1.1f, 180, 0.75f, 0.55f, 0.4f, 0.12f),
        new Item("computerScreen", 2.2f, 1.1f, 180, 0.75f, 0.55f, 0.4f, 0.12f),
        new Item("computerKeyboard", -2.2f, 1.55f, 180, 0.75f, 0.45f, 0.04f, 0.15f),
        new Item("computerKeyboard", 2.2f, 1.55f, 180, 0.75f, 0.45f, 0.04f, 0.15f),
        new Item("computerMouse", -1.85f, 1.55f, 0, 0.75f, 0.08f, 0.04f, 0.12f),
        new Item("computerMouse", 2.55f, 1.55f, 0, 0.75f, 0.08f, 0.04f, 0.12f),
        new Item("speakerSmall", -3.4f, 3.5f, 0, null, 0.3f, 0.45f, 0.3f),
        new Item("speakerSmall", 3.4f, 3.5f, 0, null, 0.3f, 0.45f, 0.3f),
        new Item("lampWall", 0.0f, 0.55f, 180, 1.9f, 0.3f, 0.15f, 0.15f),
        new Item("rugRectangle", 0.0f, 3.7f, 0, null, 3.2f, 0.02f, 1.9f),
        // —— 观影房 ——
        new Item("televisionModern", 8.0f, 0.9f, 0, null, 2.0f, 1.2f, 0.3f),
        new Item("speaker", 5.3f, 0.9f, 0, null, 0.4f, 1.0f, 0.4f),
        new Item("speaker", 10.7f, 0.9f, 0, null, 0.4f, 1.0f, 0.4f),
        new Item("loungeSofaLong", 8.0f, 2.9f, 180, null, 2.6f, 0.95f, 0.95f),
        new Item("loungeSofa", 5.8f, 4.6f, 180, null, 2.4f, 0.95f, 1.0f),
        new Item("loungeSofa", 10.2f, 4.6f, 180, null, 2.4f, 0.95f, 1.0f),
        new Item("rugRectangle", 8.0f, 3.8f, 0, null, 3.2f, 0.02f, 1.9f),
        new Item("lampSquareCeiling", 8.0f, 3.4f, 0, 2.65f, 0.5f, 0.25f, 0.5f),
    };

    [MenuItem("Tools/VRHome/摆放家具（全部）")]
    public static void PlaceAll()
    {
        int ok = 0, fail = 0;
        foreach (var it in LAYOUT)
        {
            string path = FBX_DIR + it.file + ".fbx";
            var asset = AssetDatabase.LoadAssetAtPath<GameObject>(path);
            if (asset == null)
            {
                Debug.LogWarning($"[Furniture] 缺少模型: {path}");
                fail++;
                continue;
            }
            var go = (GameObject)PrefabUtility.InstantiatePrefab(asset);
            if (go == null) { fail++; continue; }

            // 实测导入后的真实包围盒（无论 FBX 以什么比例导入都正确）
            go.transform.SetPositionAndRotation(Vector3.zero, Quaternion.identity);
            go.transform.localScale = Vector3.one;
            var rs = go.GetComponentsInChildren<Renderer>();
            if (rs.Length == 0) { Object.DestroyImmediate(go); fail++; continue; }
            Bounds b = rs[0].bounds;
            foreach (var r in rs) b.Encapsulate(r.bounds);

            Vector3 ext = b.size;
            Vector3 scale = new Vector3(
                ext.x > 0.001f ? it.tw / ext.x : 1f,
                ext.y > 0.001f ? it.th / ext.y : 1f,
                ext.z > 0.001f ? it.td / ext.z : 1f);

            // 枢轴纠偏：把"模型角枢轴"换算成目标中心落位
            Vector3 pivotOffset = -b.center;              // 本地空间：枢轴 -> 包围盒中心
            pivotOffset.x *= scale.x; pivotOffset.z *= scale.z;
            Quaternion rot = Quaternion.Euler(0f, it.ry, 0f);
            Vector3 wOffset = rot * pivotOffset;          // 旋转后的世界偏移
            float x = it.cx + wOffset.x;
            float z = it.cz + wOffset.z;
            // 高度：yov 覆盖（壁灯/镜子/吊灯/桌上物品），否则底部贴地
            float y = float.IsNaN(it.yov) ? (-b.min.y * scale.y + 0.002f) : it.yov;

            go.name = $"{PREFIX}{it.file}_{ok}";
            go.transform.SetPositionAndRotation(new Vector3(x, y, z), rot);
            go.transform.localScale = scale;
            EnsureColliders(go);
            ok++;
        }
        Debug.Log($"[Furniture] 摆放完成：成功 {ok}，失败 {fail}（尺寸为运行时实测，自动适配导入比例）");
        // 摆完立刻保存场景，避免切场景/关编辑器时丢失
        EditorSceneManager.MarkSceneDirty(EditorSceneManager.GetActiveScene());
        EditorSceneManager.SaveScene(EditorSceneManager.GetActiveScene());
        Debug.Log("[Furniture] 场景已保存");
    }

    static void EnsureColliders(GameObject go)
    {
        foreach (var mf in go.GetComponentsInChildren<MeshFilter>())
        {
            if (mf.sharedMesh == null) continue;
            if (mf.GetComponent<Collider>() != null) continue;
            var mc = mf.gameObject.AddComponent<MeshCollider>();
            mc.sharedMesh = mf.sharedMesh;
        }
    }

    [MenuItem("Tools/VRHome/清除全部家具")]
    public static void ClearAll()
    {
        int n = 0;
        foreach (var go in Object.FindObjectsOfType<GameObject>())
        {
            if (go.name.StartsWith(PREFIX))
            {
                Object.DestroyImmediate(go);
                n++;
            }
        }
        Debug.Log($"[Furniture] 已清除 {n} 件家具");
    }
}
