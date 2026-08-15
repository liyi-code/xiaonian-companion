# -*- coding: utf-8 -*-
"""
用 Unity-Skills REST 新建 VR 家场景（Assets/Scenes/VRHome.unity）：
「5室2厅」7 空间：客厅 / 练舞厅 / 厨房 / 大卧室 / 书房 / 电竞房 / 观影房。

结构：24m × 18m 单层，层高 3.2m，墙体厚 0.25m，每室独立地板贴图 + 天花板 + 点光源，
墙体留门洞/窗洞。小念（八重神子2.vrm）带全套组件出生在客厅。
家具由 FurnitureBuilder.cs（编辑器菜单）用 Kenney CC0 模型摆放。

运行前：Unity 打开任意工程 + UnitySkills REST Server 已启动。
运行：.\\venv\\Scripts\\python.exe unity_client/unity_skills/build_vr_home.py
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import unity_skills as us
except Exception as e:
    print(f"[错误] 无法加载 unity_skills 客户端：{e}")
    sys.exit(1)

URL = "http://127.0.0.1:8090"
_client = us.UnitySkills(url=URL)
us._default_client = _client
us._get_default_client = lambda: _client

SCENE = "Assets/Scenes/VRHome.unity"
TEX = "Assets/Textures_ZZZ/"
WALL_H = 3.2
WALL_T = 0.25

# 房间：name, x0, z0, x1, z1, 地板贴图, 中文名
ROOMS = [
    ("study",   -12, 0, -4,  6, "wood_floor.png",      "书房"),
    ("gaming",   -4, 0,  4,  6, "wood_floor_dark.png", "电竞房"),
    ("cinema",    4, 0, 12,  6, "cinema_floor.png",    "观影房"),
    ("bedroom", -12, 6, -4, 12, "wood_floor.png",      "大卧室"),
    ("living",   -4, 6, 12, 12, "wood_floor.png",      "客厅"),
    ("kitchen", -12, 12, -4, 18, "tile_floor.png",     "厨房"),
    ("dance",    -4, 12, 12, 18, "dance_floor.png",    "练舞厅"),
]

# 墙体：(x1,z1)-(x2,z2) 段 + 门洞/窗洞 [(起,止),...]（沿墙方向的坐标区间）
WALLS = [
    # 内墙 z=6
    ("W_z6_a", -12, 6, -4, 6, [(-9.4, -6.6)]),   # 书房↔客厅, 门 x≈-8
    ("W_z6_b",  -4, 6,  4, 6, [(-0.7, 0.7)]),    # 电竞↔客厅, 门 x=0
    ("W_z6_c",   4, 6, 12, 6, [(6.6, 9.4)]),     # 观影↔客厅, 门 x=8
    # 内墙 z=12
    ("W_z12_a", -12, 12, -4, 12, [(-9.4, -6.6)]),# 卧室↔厨房, 门 x=-8
    ("W_z12_b",  -4, 12, 12, 12, [(-0.7, 0.7)]), # 客厅↔练舞厅, 门 x=0
    # 内墙 x=-4
    ("W_x4n_a", -4, 0, -4, 6, [(2.3, 3.7)]),     # 书房↔电竞, 门 z=3
    ("W_x4n_b", -4, 6, -4, 12, [(8.3, 9.7)]),    # 卧室↔客厅, 门 z=9
    ("W_x4n_c", -4, 12, -4, 18, [(14.3, 15.7)]), # 厨房↔练舞厅, 门 z=15
    # 内墙 x=4（仅 z 0..6）
    ("W_x4p",   4, 0,  4, 6, [(2.3, 3.7)]),      # 电竞↔观影, 门 z=3
    # 外墙 x=-12（窗洞）
    ("W_oxn_a", -12, 0, -12, 6, [(2.0, 4.0)]),   # 书房窗 z=3
    ("W_oxn_b", -12, 6, -12, 12, []),
    ("W_oxn_c", -12, 12, -12, 18, [(14.0, 16.0)]),  # 厨房窗 z=15
    # 外墙 x=12（正门 + 窗洞）
    ("W_oxp_a", 12, 0, 12, 6, [(2.0, 4.0)]),     # 观影窗 z=3
    ("W_oxp_b", 12, 6, 12, 12, [(8.0, 10.0)]),   # 客厅正门 z=9
    ("W_oxp_c", 12, 12, 12, 18, [(14.3, 15.7)]), # 练舞厅门 z=15
    # 外墙 z=0
    ("W_oz0_a", -12, 0, -4, 0, [(-7.0, -5.0)]),  # 书房窗 x=-6
    ("W_oz0_b",  -4, 0,  4, 0, []),
    ("W_oz0_c",   4, 0, 12, 0, [(5.0, 7.0)]),    # 观影窗 x=6
    # 外墙 z=18
    ("W_oz18_a", -12, 18, -4, 18, [(-9.0, -7.0)]),   # 厨房窗 x=-8
    ("W_oz18_b",  -4, 18,  4, 18, []),
    ("W_oz18_c",   4, 18, 12, 18, [(3.0, 5.0)]),     # 练舞厅窗 x=4
]

def _call(skill, **kw):
    try:
        r = us.call_skill(skill, **kw)
        ok = bool(r.get("success")) if isinstance(r, dict) else bool(r)
        if not ok:
            print(f"  [x] {skill}({kw.get('name','')}): {str(r)[:140]}")
        return r
    except Exception as e:
        print(f"  [x] {skill}({kw.get('name','')}) 异常: {e}")
        return None

def _finish(name, rgb, tex=None, emissive=None, emi=1.0):
    _call("material_set_color", name=name, r=rgb[0], g=rgb[1], b=rgb[2], a=1.0)
    if tex:
        _call("material_set_texture", name=name, texturePath=TEX + tex)
    if emissive:
        _call("material_set_emission", name=name, r=emissive[0], g=emissive[1],
              b=emissive[2], intensity=emi)

def _cube(name, x, y, z, sx, sy, sz, rgb, tex=None, emissive=None, emi=1.0, ry=0.0):
    _call("gameobject_create", name=name, primitiveType="Cube", x=x, y=y, z=z)
    _call("gameobject_set_transform", name=name, posX=x, posY=y, posZ=z,
          rotY=ry, scaleX=sx, scaleY=sy, scaleZ=sz)
    _finish(name, rgb, tex, emissive, emi)
    return name

def _plane(name, x, y, z, sx, sz, rgb, tex=None):
    _call("gameobject_create", name=name, primitiveType="Plane", x=x, y=y, z=z)
    _call("gameobject_set_transform", name=name, posX=x, posY=y, posZ=z,
          scaleX=sx, scaleY=1, scaleZ=sz)
    _finish(name, rgb, tex)
    return name

def _light(name, x, y, z, rgb, intensity=1.2, rng=10):
    _call("light_create", name=name, lightType="Point", x=x, y=y, z=z, intensity=intensity)
    try:
        _call("light_set_properties", name=name, r=rgb[0], g=rgb[1], b=rgb[2],
              intensity=intensity, range=rng)
    except Exception:
        pass

def _tag(name):
    _call("component_add", name=name, componentType="PerceptTag")

def wall_segments(name, x1, z1, x2, z2, gaps):
    """沿轴线段按 gaps 切成若干墙块。gaps 为沿墙方向坐标的区间列表。"""
    if abs(x1 - x2) < 1e-6:      # 沿 z 的墙（x 固定）
        x, z0, z1 = x1, min(z1, z2), max(z1, z2)
        segs = [(z0, z1)]
        for g0, g1 in sorted(gaps):
            new = []
            for a, b in segs:
                if g1 <= a or g0 >= b:
                    new.append((a, b))
                else:
                    if a < g0: new.append((a, g0))
                    if b > g1: new.append((g1, b))
            segs = new
        for i, (a, b) in enumerate(segs):
            if b - a < 0.05: continue
            _cube(f"{name}_{i}", x, WALL_H / 2, (a + b) / 2, WALL_T, WALL_H, b - a,
                  (0.86, 0.84, 0.79), tex="wall_paint.png")
    else:                        # 沿 x 的墙（z 固定）
        z, x0, x1 = z1, min(x1, x2), max(x1, x2)
        segs = [(x0, x1)]
        for g0, g1 in sorted(gaps):
            new = []
            for a, b in segs:
                if g1 <= a or g0 >= b:
                    new.append((a, b))
                else:
                    if a < g0: new.append((a, g0))
                    if b > g1: new.append((g1, b))
            segs = new
        for i, (a, b) in enumerate(segs):
            if b - a < 0.05: continue
            _cube(f"{name}_{i}", (a + b) / 2, WALL_H / 2, z, b - a, WALL_H, WALL_T,
                  (0.86, 0.84, 0.79), tex="wall_paint.png")

def build():
    print("[VR家] 等待 Unity REST Server ...")
    if not us.wait_for_health(timeout=120):
        print("[错误] UnitySkills Server 未就绪")
        return
    print("[VR家] 1/5 新建场景 VRHome.unity")
    _call("scene_create", scenePath=SCENE)

    print("[VR家] 2/5 地板（每室独立贴图）")
    for rid, x0, z0, x1, z1, tex, zh in ROOMS:
        cx, cz = (x0 + x1) / 2, (z0 + z1) / 2
        _plane(f"Room_{rid}", cx, 0.01, cz, (x1 - x0) / 10.0, (z1 - z0) / 10.0,
               (0.8, 0.78, 0.74), tex=tex)
        _tag(f"Room_{rid}")
        print(f"  · {zh}")

    print("[VR家] 3/5 墙体（含门洞/窗洞）")
    for name, x1, z1, x2, z2, gaps in WALLS:
        wall_segments(name, x1, z1, x2, z2, gaps)

    print("[VR家] 4/5 天花板 + 灯光")
    for cx, cz in ((-6, 4.5), (6, 4.5), (-6, 13.5), (6, 13.5)):
        _plane(f"Ceiling_{cx}_{cz}", cx, WALL_H, cz, 1.2, 0.9, (0.95, 0.93, 0.89),
               tex="ceiling_white.png")
    _light("Light_study", -8, 2.6, 3, (1.0, 0.96, 0.86), 1.1)
    _light("Light_gaming", 0, 2.6, 3, (0.7, 0.8, 1.0), 1.0)
    _light("Light_cinema", 8, 2.6, 3, (0.9, 0.85, 1.0), 0.8)
    _light("Light_bedroom", -8, 2.6, 9, (1.0, 0.92, 0.8), 1.0)
    _light("Light_living_a", -2, 2.6, 9, (1.0, 0.95, 0.85), 1.2)
    _light("Light_living_b", 6, 2.6, 9, (1.0, 0.95, 0.85), 1.2)
    _light("Light_kitchen", -8, 2.6, 15, (1.0, 0.98, 0.9), 1.2)
    _light("Light_dance", 4, 2.6, 15, (0.95, 0.9, 1.0), 1.3)

    print("[VR家] 5/5 小念（八重神子2.vrm）出生在客厅")
    r = _call("prefab_instantiate", prefabPath="Assets/八重神子2.vrm",
              x=0, y=0, z=9, name="NPC_xiaonian")
    if r and r.get("success"):
        for comp in ("NpcBridgeClient", "ExpressionController", "AgentController",
                     "SymbolicPerception", "ConceptStateMachine", "ActionRecorder"):
            _call("component_add", name="NPC_xiaonian", componentType=comp)
    # 桌面调试相机（Empty + Camera + AudioListener）
    _call("gameobject_create", name="Camera", primitiveType="Empty", x=0, y=1.7, z=16.5)
    _call("component_add", name="Camera", componentType="Camera")
    _call("component_add", name="Camera", componentType="AudioListener")
    _call("component_add", name="Camera", componentType="PlayerChatController")
    _call("gameobject_set_transform", name="Camera", posX=0, posY=1.7, posZ=16.5,
          rotX=10, rotY=180)

    _call("scene_save", scenePath=SCENE)
    print("\n✅ VR 家结构完成：Assets/Scenes/VRHome.unity")
    print("   下一步：Unity 菜单 Tools > VRHome > 摆放家具（Kenney 模型已导入）")

if __name__ == "__main__":
    build()
