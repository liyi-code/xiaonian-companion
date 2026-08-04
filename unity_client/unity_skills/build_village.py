# -*- coding: utf-8 -*-
"""
用 Unity-Skills v2.4.2 客户端驱动 Unity 编辑器，一键搭建「我的世界村庄」式自给自足小镇场景。

前置（你那侧操作）：
  1. 在 Unity 工程里装好 Unity-Skills（v2.x，776 个 skill 版本）+ UniVRM。
  2. 菜单 Window > UnitySkills，点顶部开关启动 REST Server（默认 http://localhost:8090）。
  3. 把 VRM 模型放进工程 Assets/ 根（脚本默认用 Assets/yixuan3.vrm 与 Assets/八重神子2.vrm）。
  4. 保持 Unity 编辑器打开，运行本脚本：
     venv/Scripts/python.exe unity_client/unity_skills/build_village.py

本脚本会：建场景 -> 地面/光照 -> 摆 7 栋彩色建筑方块
          -> 实例化 6 个 VRM 村民（prefab_instantiate 直接吃 .vrm，挂在对应建筑下）
          -> 烘焙 NavMesh，让村民能走动。

说明：NPC 角色现在直接用 .vrm 模型实例化（不再是立方体/胶囊占位），
      因为 prefab_instantiate 在 v2.4.2 已支持直接加载 VRM 资源。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import unity_skills as us
except Exception as e:
    print(f"[错误] 无法加载 unity_skills 客户端：{e}")
    sys.exit(1)

# 强制直连 UnitySkills Server（v2.4.2），并 patch 内部 _get_default_client，
# 因为 wait_for_health 内部会清空 _default_client 再重建。
URL = "http://127.0.0.1:8090"
try:
    _client = us.UnitySkills(url=URL)
    us._default_client = _client
    us._get_default_client = lambda: _client
    print(f"[初始化] 已直连 UnitySkills Server @ {URL}")
except Exception as e:
    print(f"[警告] 直连失败：{e}")

# 建筑布局（与 src/town.py BUILDINGS 对齐）：id -> (中文名, x, z, RGB 0-1)
BUILDINGS = [
    ("farm",        "农田",   -8,  6, (0.42, 0.69, 0.31)),
    ("lumber_mill", "伐木场",  8,  6, (0.55, 0.35, 0.17)),
    ("mine",        "矿洞",   -8, -6, (0.33, 0.33, 0.33)),
    ("kitchen",     "厨房",    0,  0, (0.79, 0.48, 0.29)),
    ("market",      "市集",    8, -6, (0.88, 0.75, 0.41)),
    ("forge",       "铁匠铺",  0,  8, (0.60, 0.23, 0.23)),
    ("well",        "水井",    0, -8, (0.29, 0.56, 0.85)),
]

# 村民（与 src/villagers.py 对齐）：npc_id, 名字, 职业, 建筑id, vrm资源路径
# 注意：工程里只有 2 个 VRM，小念用 yixuan3，其余用八重神子2（你只有这 2 个模型）。
VRM_XIAONIAN = "Assets/yixuan3.vrm"
VRM_YAE       = "Assets/八重神子2.vrm"
NPCS = [
    ("xiaonian", "小念", "farmer",     "farm",        VRM_XIAONIAN),
    ("linn",     "琳恩", "lumberjack", "lumber_mill", VRM_YAE),
    ("kai",      "凯",   "miner",      "mine",        VRM_YAE),
    ("mira",     "米拉", "cook",       "kitchen",     VRM_YAE),
    ("toby",     "托比", "merchant",   "market",      VRM_YAE),
    ("roe",      "罗伊", "smith",      "forge",       VRM_YAE),
]


def _pos(name):
    for bid, zh, x, z, _ in BUILDINGS:
        if bid == name:
            return x, z
    return 0, 0


def _zh(bid):
    for b, zh, *_ in BUILDINGS:
        if b == bid:
            return zh
    return bid


def _cleanup_tests():
    """删除之前探测遗留的测试对象（VRMTestA / VRMChildTest 等）。"""
    for t in ("VRMTestA", "VRMChildTest"):
        r = _call("gameobject_delete", name=t)
        if r and r.get("success"):
            print(f"[清理] 已删除测试对象 {t}")


def _call(skill_name, **kw):
    """包装 call_skill，打印 Server 返回（含真实路径/错误信息）。"""
    try:
        res = us.call_skill(skill_name, **kw)
        print(f"  -> {skill_name}: {str(res)[:300]}")
        return res
    except Exception as e:
        print(f"  -> {skill_name} 异常: {e}")
        return None


def build():
    print("[搭建] 等待 Unity REST Server 就绪...")
    health = us.wait_for_health(timeout=120)
    if not health:
        print("[错误] 未检测到 Unity Server，请先在 Unity 里启动 UnitySkills Server。")
        return
    print(f"[搭建] Unity 已就绪：{health.get('unityVersion') or health}")

    # 0) 清理上次探测遗留的测试对象
    _cleanup_tests()

    # 1) 新建场景
    scene_path = "Assets/Scenes/TownScene.unity"
    _call("scene_create", scenePath=scene_path)
    print(f"[搭建] 场景 {scene_path} 已建")

    # 2) 地面（大平面）
    _call("gameobject_create", name="Ground", primitiveType="Plane", x=0, y=0, z=0)
    _call("gameobject_set_transform", name="Ground",
          scaleX=20, scaleY=1, scaleZ=20)
    # 直接给物体上色（material_set_color 的 name 是物体名，无需先建材质落盘）
    _call("material_set_color", name="Ground", r=0.24, g=0.56, b=0.25, a=1.0)
    print("[搭建] 地面 Ground 已建")

    # 3) 方向光
    _call("light_create", name="SunLight", lightType="Directional",
          x=5, y=10, z=5, intensity=1.2)
    print("[搭建] 光照 SunLight 已建")

    # 4) 每栋建筑：彩色方块
    for bid, zh, x, z, rgb in BUILDINGS:
        go_name = f"Building_{zh}"
        _call("gameobject_create", name=go_name, primitiveType="Cube",
              x=x, y=1, z=z)
        _call("gameobject_set_transform", name=go_name,
              posX=x, posY=1, posZ=z,
              scaleX=3, scaleY=2, scaleZ=3)
        _call("material_set_color", name=go_name,
              r=rgb[0], g=rgb[1], b=rgb[2], a=1.0)
        print(f"[搭建] 建筑 {zh} @ ({x},{z})")

    # 5) 每个村民：直接实例化 VRM 模型（不再是立方体/胶囊占位）
    for nid, nm, role, bld, vrm in NPCS:
        bx, bz = _pos(bld)
        px, pz = bx + 3.5, bz
        npc_name = f"NPC_{nid}"
        # prefab_instantiate 在 v2.4.2 支持直接加载 .vrm，并挂到对应建筑下
        res = _call("prefab_instantiate", prefabPath=vrm,
                    x=px, y=0, z=pz, name=npc_name, parentName=f"Building_{_zh(bld)}")
        if res and res.get("success"):
            # 挂 3D 前端脚本：WebSocket 客户端 + 表情 + 概念状态机
            # （脚本需在 Unity 已编译后才会被识别；失败静默跳过，可手动挂）
            for comp in ("NpcBridgeClient", "ExpressionController", "ConceptStateMachine"):
                r = _call("component_add", name=npc_name, componentType=comp)
                if not (r and r.get("success")):
                    print(f"[提示] {nm} 挂 {comp} 未成功（请确认脚本已编译，或手动挂）")
            # npcId 由物体名 NPC_<id> 自动推导（NpcBridgeClient.Start 里解析），无需额外设字段
            print(f"[搭建] 村民 {nm}({role}, id={nid}) VRM 已实例化 @ ({px},{pz})")
        else:
            print(f"[警告] 村民 {nm} VRM 实例化失败，回退为胶囊占位")
            _call("gameobject_create", name=npc_name, primitiveType="Capsule",
                  x=px, y=1, z=pz)
            _call("gameobject_set_transform", name=npc_name,
                  posX=px, posY=1, posZ=pz, scaleX=1, scaleY=1, scaleZ=1)

    # 6) NavMesh（让村民可寻路走动）
    _call("navmesh_bake")
    print("[搭建] NavMesh 已烘焙")

    # 7) 保存场景
    _call("scene_save", scenePath=scene_path)
    print("[搭建] 场景已保存")

    print("\n✅ 村庄场景搭建完成！")
    print("   回到 Unity 编辑器，Project 面板打开 Assets/Scenes/TownScene.unity 即可看到村庄。")
    print("   村民已是真实 VRM 模型（小念= yixuan3，其余= 八重神子2），挂在各自建筑下。")
    print("   运行 bridge（venv\\Scripts\\python.exe -m src.bridge）即可看到小镇联动。")


if __name__ == "__main__":
    build()
