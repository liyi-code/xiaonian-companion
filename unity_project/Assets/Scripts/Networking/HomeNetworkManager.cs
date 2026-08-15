// HomeNetworkManager.cs —— VR 家联机管理器（Photon Fusion 2，Shared 模式）
// ============================================================================
// 挂在 NetworkStart 场景的 NetworkBootstrap 物体上（NetworkSetup 一键生成）。
// 启动即自动「加入或创建」房间 XiaoNianHome，并把 VRHome 场景叠加加载进来。
// 每位玩家加入时为自己的 LocalPlayer 生成 NetworkPlayer 预制体（每人控制自己）。
// 小念的网络同步（位置/动作/气泡广播）由后续 NetworkXiaonianRelay 负责。
// ============================================================================
using System;
using System.Collections.Generic;
using Fusion;
using Fusion.Sockets;
using UnityEngine;
using UnityEngine.SceneManagement;

public class HomeNetworkManager : Fusion.Behaviour, INetworkRunnerCallbacks
{
    [Header("Photon")]
    [Tooltip("留空则使用 Fusion Hub 的 PhotonAppSettings 资产；如需代码覆盖再填")]
    public string AppIdFusion = "";
    public string SessionName = "XiaoNianHome";
    public string HomeScenePath = "Assets/Scenes/VRHome.unity";
    public int MaxPlayers = 8;

    [Header("预制体")]
    public NetworkObject PlayerPrefab;

    NetworkRunner _runner;
    bool _localSpawned;
    bool _sceneLoaded;

    async void Start()
    {
        _runner = GetComponent<NetworkRunner>();
        if (_runner == null) _runner = gameObject.AddComponent<NetworkRunner>();
        if (GetComponent<INetworkSceneManager>() == null)
            gameObject.AddComponent<NetworkSceneManagerDefault>();
        if (GetComponent<INetworkObjectProvider>() == null)
            gameObject.AddComponent<NetworkObjectProviderDefault>();

        var sceneInfo = new NetworkSceneInfo();
        int idx = SceneUtility.GetBuildIndexByScenePath(HomeScenePath);
        if (idx >= 0)
            sceneInfo.AddSceneRef(SceneRef.FromIndex(idx), LoadSceneMode.Additive);
        else
            Debug.LogError($"[联机] 场景不在 Build Settings 里: {HomeScenePath}");

        // App ID 走标准路径：Fusion Hub 的 PhotonAppSettings 资产
        // （Assets/Photon/Fusion/Resources/PhotonAppSettings.asset，已配置 AppIdFusion）。
        Debug.Log($"[联机] 正在进入房间 {SessionName} ...");
        var res = await _runner.StartGame(new StartGameArgs
        {
            GameMode = GameMode.Shared,
            Address = NetAddress.Any(),
            SessionName = SessionName,
            Scene = sceneInfo,
            SceneManager = GetComponent<INetworkSceneManager>(),
            ObjectProvider = GetComponent<INetworkObjectProvider>(),
            PlayerCount = MaxPlayers,
        });

        if (res.Ok)
        {
            Debug.Log($"[联机] 已进入家：{SessionName}（Shared 模式，最多 {MaxPlayers} 人）");
        }
        else
        {
            Debug.LogError($"[联机] 进入失败：{res.ShutdownReason}。请检查 App ID 与网络。");
        }
    }

    void OnDestroy()
    {
        if (_runner != null && _runner.IsRunning)
            _runner.Shutdown();
    }

    // ---- INetworkRunnerCallbacks ----
    public void OnPlayerJoined(NetworkRunner runner, PlayerRef player)
    {
        if (player == runner.LocalPlayer)
        {
            Debug.Log($"[联机] 玩家 {player.PlayerId} 加入");
            TrySpawnLocal(runner);
        }
        else
        {
            Debug.Log($"[联机] 玩家 {player.PlayerId} 加入房间");
        }
    }

    public void OnSceneLoadDone(NetworkRunner runner)
    {
        Debug.Log("[联机] VRHome 场景加载完成");
        _sceneLoaded = true;
        TrySpawnLocal(runner);
    }

    /// <summary>
    /// 关键：必须等网络场景（VRHome）加载完成后再生成化身，
    /// 否则化身+相机会生成在空的入口场景里，Game 视图没有视角。
    /// </summary>
    void TrySpawnLocal(NetworkRunner runner)
    {
        if (_localSpawned || PlayerPrefab == null || !_sceneLoaded) return;

        // 出生点：客厅沙发旁（y=1.0 悬空一点，CharacterController 会落回地板，避免出生陷进地面穿模）
        var pos = new Vector3(0f, 1.0f, 8f) + UnityEngine.Random.insideUnitSphere * 0.5f;
        pos.y = 1.0f;
        runner.Spawn(PlayerPrefab, pos, Quaternion.identity, runner.LocalPlayer);
        _localSpawned = true;
        Debug.Log($"[联机] 本地化身已生成 @ {pos}");
    }

    public void OnPlayerLeft(NetworkRunner runner, PlayerRef player)
    {
        Debug.Log($"[联机] 玩家 {player.PlayerId} 离开");
    }

    public void OnInput(NetworkRunner runner, NetworkInput input) { }
    public void OnInputMissing(NetworkRunner runner, PlayerRef player, NetworkInput input) { }
    public void OnShutdown(NetworkRunner runner, ShutdownReason shutdownReason)
    {
        Debug.Log($"[联机] 已断开：{shutdownReason}");
        _sceneLoaded = false;
        _localSpawned = false;
    }
    public void OnConnectedToServer(NetworkRunner runner) { }
    public void OnDisconnectedFromServer(NetworkRunner runner, NetDisconnectReason reason) { }
    public void OnConnectRequest(NetworkRunner runner, NetworkRunnerCallbackArgs.ConnectRequest request, byte[] token) { }
    public void OnConnectFailed(NetworkRunner runner, NetAddress remoteAddress, NetConnectFailedReason reason) { }
    public void OnUserSimulationMessage(NetworkRunner runner, SimulationMessagePtr message) { }
    public void OnSessionListUpdated(NetworkRunner runner, List<SessionInfo> sessionList) { }
    public void OnCustomAuthenticationResponse(NetworkRunner runner, Dictionary<string, object> data) { }
    public void OnHostMigration(NetworkRunner runner, HostMigrationToken hostMigrationToken) { }
    public void OnSceneLoadStart(NetworkRunner runner) { }
    public void OnReliableDataReceived(NetworkRunner runner, PlayerRef player, ReliableKey key, ReadOnlySpan<byte> data) { }
    public void OnReliableDataProgress(NetworkRunner runner, PlayerRef player, ReliableKey key, float progress) { }
    public void OnStateAuthorityChanged(NetworkRunner runner, NetworkObject obj, bool isStateAuthority) { }
    public void OnObjectEnterAOI(NetworkRunner runner, NetworkObject obj, PlayerRef player) { }
    public void OnObjectExitAOI(NetworkRunner runner, NetworkObject obj, PlayerRef player) { }
    public void OnDisconnectedFromServer(NetworkRunner runner, NetDisconnectReason reason, NetAddress remoteAddress) { }
}
