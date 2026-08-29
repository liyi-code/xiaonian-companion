// NetworkSetup.cs —— VR 家联机一键配置（编辑器工具）
// ============================================================================
// 菜单 Tools > VRHome > 一键配置联机（Fusion）：
//   1) 生成玩家预制体 Assets/Prefabs/NetworkPlayer.prefab
//      （胶囊 + NetworkObject + NetworkTransform + NetworkPlayerSync + 相机）
//   2) 创建联机入口场景 Assets/Scenes/NetworkStart.unity
//      （NetworkRunner + HomeNetworkManager + 场景管理器）
//   3) 把 VRHome + NetworkStart 加入 Build Settings
//   4) 提示手动一步：把 NetworkPlayer 预制体拖进 Fusion 的 Prefabs 表
//      （Fusion Hub → Realtime Settings → Prefabs → 拖入；官方 UI，最稳）
// ============================================================================
using Fusion;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

public static class NetworkSetup
{
    const string PREFAB_PATH = "Assets/Prefabs/NetworkPlayer.prefab";
    const string START_SCENE = "Assets/Scenes/NetworkStart.unity";
    const string HOME_SCENE = "Assets/Scenes/VRHome.unity";

    [MenuItem("Tools/VRHome/一键配置联机（Fusion）")]
    public static void Run()
    {
        // ---- 1) 玩家预制体（过渡期用 yixuan3 VRM 当玩家模型）----
        if (!AssetDatabase.IsValidFolder("Assets/Prefabs"))
            AssetDatabase.CreateFolder("Assets", "Prefabs");

        GameObject source = null;
        var vrm = AssetDatabase.LoadAssetAtPath<GameObject>("Assets/yixuan3.vrm");
        if (vrm != null)
        {
            source = (GameObject)PrefabUtility.InstantiatePrefab(vrm);
            source.name = "NetworkPlayer";
            source.transform.SetPositionAndRotation(Vector3.zero, Quaternion.identity);
            // 清掉 VRM 自带碰撞体，统一用 CharacterController
            foreach (var c in source.GetComponentsInChildren<Collider>())
                Object.DestroyImmediate(c);
            Debug.Log("[NetworkSetup] 玩家模型：yixuan3.vrm");
        }
        else
        {
            source = GameObject.CreatePrimitive(PrimitiveType.Capsule);
            source.name = "NetworkPlayer";
            source.transform.localScale = new Vector3(0.6f, 0.9f, 0.6f);
            Debug.LogWarning("[NetworkSetup] 未找到 yixuan3.vrm，回退胶囊");
        }

        source.AddComponent<NetworkObject>();
        var ncc = source.AddComponent<NetworkCharacterController>();   // Shared 模式玩家移动专用
        if (ncc != null)
        {
            ncc.maxSpeed = 2.2f;       // 走路速度（米/秒）
            ncc.acceleration = 6f;     // 加速：低一点起步更柔和
            ncc.braking = 8f;          // 刹车：松手即停，不发飘
            ncc.rotationSpeed = 0f;    // 关闭"自动面朝移动方向"：A/D 平移不转身，朝向只由鼠标控制
        }
        // NCC 的移动体是 CharacterController。VRM 根上可能已有冲突组件（Rigidbody 等），先清再挂：
        var rb = source.GetComponent<Rigidbody>();
        if (rb != null) Object.DestroyImmediate(rb);
        var cc = source.GetComponent<CharacterController>();
        if (cc == null) cc = source.AddComponent<CharacterController>();
        if (cc == null)
        {
            Debug.LogError("[NetworkSetup] CharacterController 添加失败，预制体中止");
            Object.DestroyImmediate(source);
            return;
        }
        cc.height = 1.5f;
        cc.radius = 0.3f;
        cc.center = new Vector3(0f, 0.75f, 0f);
        cc.skinWidth = 0.06f;      // 薄皮肤：减少卡边角时的爆炸性推挤
        cc.stepOffset = 0f;        // 取消“自动攀爬/自动上台阶”：只有按空格才离地
        cc.slopeLimit = 50f;
        source.AddComponent<NetworkPlayerSync>();
        source.AddComponent<PlayerChatController>();         // 本机聊天入口（找小念对话）
        source.AddComponent<ConceptStateMachine>();          // 玩家化身生命感：呼吸 + 待机转头 + 手臂自然垂放（告别十字站姿）
        source.AddComponent<VrFullBodyTracking>();           // VR 全身捕捉（头+双手，髋/脚追踪器自动发现；桌面自动休眠）
        source.AddComponent<VoiceCapture>();                 // 语音采集（按住 V / VR 扳机说话 → RPC 到主机）

        var camGo = new GameObject("PlayerCamera");
        camGo.transform.SetParent(source.transform, false);
        camGo.transform.localPosition = new Vector3(0f, 1.45f, 0f);
        camGo.AddComponent<Camera>();
        camGo.AddComponent<AudioListener>();

        var prefab = PrefabUtility.SaveAsPrefabAsset(source, PREFAB_PATH);
        Object.DestroyImmediate(source);
        Debug.Log($"[NetworkSetup] 玩家预制体已生成: {PREFAB_PATH}");

        // ---- 2) 联机入口场景 ----
        var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
        var boot = new GameObject("NetworkBootstrap");
        boot.AddComponent<NetworkRunner>();
        boot.AddComponent<NetworkSceneManagerDefault>();
        boot.AddComponent<NetworkObjectProviderDefault>();
        var mgr = boot.AddComponent<HomeNetworkManager>();
        mgr.PlayerPrefab = prefab != null ? prefab.GetComponent<NetworkObject>() : null;
        EditorSceneManager.SaveScene(scene, START_SCENE);
        Debug.Log($"[NetworkSetup] 联机入口场景已创建: {START_SCENE}");

        // ---- 3) Build Settings ----
        EditorBuildSettings.scenes = new[]
        {
            new EditorBuildSettingsScene(HOME_SCENE, true),
            new EditorBuildSettingsScene(START_SCENE, true),
        };
        Debug.Log("[NetworkSetup] Build Settings 已更新（VRHome + NetworkStart）");

        EditorSceneManager.OpenScene(START_SCENE);
        Debug.Log("✅ 联机配置完成，还剩最后一步（必须）：\n" +
                  "   菜单 Fusion → Fusion Hub → Realtime Settings → Prefabs 列表 →\n" +
                  "   把 Assets/Prefabs/NetworkPlayer.prefab 拖进去 → 保存。\n" +
                  "   然后 ▶ Play：自动进家（Shared 模式）。");
    }
}
