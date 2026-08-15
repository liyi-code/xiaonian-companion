# -*- coding: utf-8 -*-
"""
用 Unity-Skills REST 在「当前打开的 TownScene」里重建小镇场景：
风格仿《绝区零》六分街 —— 一条霓虹商业街，两侧店铺 + 中央水井小广场。

与 build_village.py 的区别：
  * 不 scene_create 新建场景：在当前打开的场景里施工，保留 NPC / Camera / BridgeHub。
  * 街区长约 22m、宽约 17m（建筑中心距原点最远 10.3m < 感知半径 14m），NPC 全覆盖。
  * 每栋建筑 = 主体(程序化贴图) + 屋顶 + 店门 + 两扇发光窗 + 中文霓虹招牌 + 条纹雨棚。
  * 贴图由 make_zzz_textures.py 生成在 Assets/Textures_ZZZ/（首次请先运行它）。
  * 脚本可重复运行：先删除上一轮生成的全部街道部件（只删本脚本命名的对象，不碰 NPC_*）。

前置：
  1. Unity 打开 unity_project 与 Assets/Scenes/TownScene.unity。
  2. Window > UnitySkills 启动 REST Server（默认 http://localhost:8090）。
  3. 首次运行先执行一次：python unity_client/unity_skills/make_zzz_textures.py
  4. 运行：
       .\\venv\\Scripts\\python.exe unity_client/unity_skills/build_sixth_street.py
     可选参数：--no-save 只施工不保存；--keep-ground 不重建地面。

坐标体系：车行道 x∈[-2.5,2.5]，人行道 x∈[2.5,4.5]/[-4.5,-2.5]，建筑中心 x=±6.5。
  西侧：farm 农田温室(z=8) / kitchen 拉面馆(z=0) / mine 矿洞(z=-8)
  东侧：lumber_mill 木材店(z=8) / market 便利店(z=0) / forge 铁匠铺(z=-8)
  水井 well 在街道中央 (0,0,0)。
  与 src/town.py BUILDINGS、Assets/Scripts/TownLayout.cs 严格对齐。
"""
import sys, os, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import unity_skills as us
except Exception as e:
    print(f"[错误] 无法加载 unity_skills 客户端：{e}")
    sys.exit(1)

URL = "http://127.0.0.1:8090"
try:
    _client = us.UnitySkills(url=URL)
    us._default_client = _client
    us._get_default_client = lambda: _client
    print(f"[初始化] 已直连 UnitySkills Server @ {URL}")
except Exception as e:
    print(f"[警告] 直连失败：{e}")

SCENE_PATH = "Assets/Scenes/TownScene.unity"
TEX = "Assets/Textures_ZZZ/"

# ---------------------------------------------------------------- 布局表
# id, 中文名, x, z, 面向(+1 朝东行车道), 主体色, 主体贴图, 雨棚色, 雨棚贴图, 霓虹色, 招牌贴图, 窗光色
BUILDINGS = [
    ("farm",        "农田温室", -6.5,  8, +1, (0.30, 0.44, 0.30), "brick_green.png",
     (0.35, 0.62, 0.32), "awning_farm.png",   (0.25, 0.95, 0.35), "sign_farm.png",   (0.95, 1.00, 0.75)),
    ("kitchen",     "拉面馆",   -6.5,  0, +1, (0.33, 0.22, 0.16), "brick_red.png",
     (0.80, 0.14, 0.12), "awning_kitchen.png",(1.00, 0.32, 0.10), "sign_kitchen.png",(1.00, 0.78, 0.45)),
    ("mine",        "矿洞",     -6.5, -8, +1, (0.13, 0.13, 0.16), "stone.png",
     (0.24, 0.24, 0.22), "awning_mine.png",  (0.95, 0.85, 0.15), "sign_mine.png",   (0.85, 0.60, 0.30)),
    ("lumber_mill", "木材店",    6.5,  8, -1, (0.30, 0.20, 0.12), "planks.png",
     (0.42, 0.28, 0.16), "awning_lumber.png",(1.00, 0.60, 0.15), "sign_lumber.png", (0.95, 0.75, 0.45)),
    ("market",      "便利店",    6.5,  0, -1, (0.12, 0.30, 0.32), "brick_teal.png",
     (0.14, 0.72, 0.78), "awning_market.png",(0.20, 0.90, 1.00), "sign_market.png", (0.75, 0.95, 1.00)),
    ("forge",       "铁匠铺",    6.5, -8, -1, (0.16, 0.16, 0.19), "brick_dark.png",
     (0.44, 0.12, 0.10), "awning_forge.png", (1.00, 0.42, 0.05), "sign_forge.png",  (1.00, 0.45, 0.15)),
]

OLD_VILLAGE = ["Building_农田", "Building_伐木场", "Building_矿洞",
               "Building_厨房", "Building_市集", "Building_铁匠铺", "Building_水井"]

ROAD_RGB = (0.16, 0.17, 0.20)
SIDEWALK_RGB = (0.34, 0.36, 0.42)
LAMP_WARM = (1.0, 0.82, 0.45)

# 本脚本生成的全部对象名（用于重跑时清理；不含 NPC_*）
def _own_names():
    names = ["Ground", "Road", "Sidewalk_W", "Sidewalk_E"]
    for bid, *_ in BUILDINGS:
        names += [f"B_{bid}_body", f"B_{bid}_roof", f"B_{bid}_door",
                  f"B_{bid}_win0", f"B_{bid}_win1", f"B_{bid}_sign", f"B_{bid}_awn"]
    names += [f"B_farm_crop{i}" for i in range(6)]
    names += [f"B_lumber_log{i}" for i in range(3)]
    names += [f"B_mine_rock{i}" for i in range(3)]
    names += ["B_forge_chimney", "B_forge_fire",
              "B_market_vend0", "B_market_vend0_panel",
              "B_market_vend1", "B_market_vend1_panel",
              "B_well_ring", "B_well_post1", "B_well_post2", "B_well_roof", "B_well_lantern"]
    for side in ("W", "E"):
        for i in range(3):
            names += [f"Lamp_{side}{i}_pole", f"Lamp_{side}{i}_head", f"Lamp_{side}{i}_light"]
        names += [f"Bench_{side}", f"Bench_{side}_back"]
    for s in ("S", "N"):
        names += [f"Gate_{s}_beam", f"Gate_{s}_p1", f"Gate_{s}_p2"]
    names += [f"Cross_zebra{i}" for i in range(4)]
    return names


# ---------------------------------------------------------------- REST 封装
def _call(skill_name, **kw):
    try:
        res = us.call_skill(skill_name, **kw)
        ok = bool(res.get("success")) if isinstance(res, dict) else bool(res)
        if not ok:
            print(f"  [x] {skill_name}({kw.get('name', '')}): {str(res)[:160]}")
        return res
    except Exception as e:
        print(f"  [x] {skill_name}({kw.get('name', '')}) 异常: {e}")
        return None


def _finish(name, rgb, emissive=None, emi_int=1.0, tex=None):
    _call("material_set_color", name=name, r=rgb[0], g=rgb[1], b=rgb[2], a=1.0)
    if tex:
        _call("material_set_texture", name=name, texturePath=TEX + tex)
    if emissive:
        _call("material_set_emission", name=name,
              r=emissive[0], g=emissive[1], b=emissive[2], intensity=emi_int)


def _cube(name, x, y, z, sx, sy, sz, rgb, emissive=None, emi_int=1.0, tex=None):
    _call("gameobject_create", name=name, primitiveType="Cube", x=x, y=y, z=z)
    _call("gameobject_set_transform", name=name,
          posX=x, posY=y, posZ=z, scaleX=sx, scaleY=sy, scaleZ=sz)
    _finish(name, rgb, emissive, emi_int, tex)
    return name


def _cyl(name, x, y, z, sx, sy, sz, rgb, emissive=None, emi_int=1.0, rx=0.0, tex=None):
    _call("gameobject_create", name=name, primitiveType="Cylinder", x=x, y=y, z=z)
    _call("gameobject_set_transform", name=name,
          posX=x, posY=y, posZ=z, rotX=rx, scaleX=sx, scaleY=sy, scaleZ=sz)
    _finish(name, rgb, emissive, emi_int, tex)
    return name


def _sphere(name, x, y, z, d, rgb, emissive=None, emi_int=1.0):
    _call("gameobject_create", name=name, primitiveType="Sphere", x=x, y=y, z=z)
    _call("gameobject_set_transform", name=name,
          posX=x, posY=y, posZ=z, scaleX=d, scaleY=d, scaleZ=d)
    _finish(name, rgb, emissive, emi_int)
    return name


def _plane(name, x, y, z, sx, sz, rgb, tex=None):
    _call("gameobject_create", name=name, primitiveType="Plane", x=x, y=y, z=z)
    _call("gameobject_set_transform", name=name,
          posX=x, posY=y, posZ=z, scaleX=sx, scaleY=1, scaleZ=sz)
    _finish(name, rgb, tex=tex)
    return name


def _light(name, x, y, z, rgb, intensity=1.0):
    _call("light_create", name=name, lightType="Point", x=x, y=y, z=z,
          intensity=intensity)
    try:
        _call("light_set_properties", name=name, r=rgb[0], g=rgb[1], b=rgb[2],
              intensity=intensity, range=7)
    except Exception:
        pass


def _delete(name):
    _call("gameobject_delete", name=name)


def _tag(name):
    _call("component_add", name=name, componentType="PerceptTag")


# ---------------------------------------------------------------- 建筑
def build_facade(bid, zh, x, z, f, body, body_tex, awn, awn_tex, sign, sign_tex, win):
    """标准店铺立面：主体(贴图)+屋顶+门+两扇发光窗+中文霓虹招牌+条纹雨棚。f=±1 面朝街道。"""
    p = f"{bid}"
    _cube(f"B_{p}_body", x, 1.6, z, 5, 3.2, 4, body, tex=body_tex)
    _cube(f"B_{p}_roof", x, 3.42, z, 5.6, 0.45, 4.6, tuple(c * 0.55 for c in body), tex="roof_dark.png")
    _cube(f"B_{p}_door", x + f * 2.28, 1.0, z, 1.3, 2.0, 0.3, (0.30, 0.28, 0.26), tex="door_dark.png")
    for i, dz in enumerate((-1.55, 1.55)):
        _cube(f"B_{p}_win{i}", x + f * 2.28, 1.75, z + dz, 1.5, 1.15, 0.12,
              (0.35, 0.28, 0.20), emissive=win, emi_int=0.55, tex="glass_warm.png")
    _cube(f"B_{p}_sign", x + f * 2.38, 2.62, z, 3.2, 0.85, 0.25,
          (0.10, 0.09, 0.12), emissive=sign, emi_int=1.1, tex=sign_tex)
    _cube(f"B_{p}_awn", x + f * 1.72, 2.06, z, 4.4, 0.15, 1.6, awn, tex=awn_tex)
    _tag(f"B_{p}_body")


def build_specials():
    # farm：温室后侧菜地
    for i in range(6):
        _cube(f"B_farm_crop{i}", -6.5 + (i - 2.5) * 0.8, 0.16, 10.5, 0.7, 0.32, 0.7,
              (0.22, 0.55, 0.24))
    # lumber：门前横放 3 根原木
    for i, dz in enumerate((-1.2, 0, 1.2)):
        _cyl(f"B_lumber_log{i}", 4.3, 0.28, 8 + dz, 0.3, 1.0, 0.3,
             (0.44, 0.30, 0.17), rx=90)
    # mine：门前碎石堆
    for i, (dx, dz, dd) in enumerate(((-4.2, -9.4, 0.55), (-4.7, -10.3, 0.4), (-3.8, -10.6, 0.45))):
        _sphere(f"B_mine_rock{i}", dx, dd * 0.5, dz, dd, (0.30, 0.30, 0.33))
    # forge：烟囱 + 炉火窗
    _cube("B_forge_chimney", 6.5, 4.35, -6.5, 0.5, 1.4, 0.5, (0.20, 0.12, 0.10))
    _cube("B_forge_fire", 4.28, 1.0, -8, 0.9, 0.9, 0.12, (0.30, 0.18, 0.10),
          emissive=(1.0, 0.30, 0.05), emi_int=1.5, tex="glass_warm.png")
    # 便利店旁两台自动贩卖机（ZZZ 标志物，贴图正面）
    for i, dx in enumerate((5.0, 6.35)):
        _cube(f"B_market_vend{i}", dx, 1.05, -2.6, 1.0, 2.1, 0.9, (0.16, 0.18, 0.28), tex="vend_front.png")
        _cube(f"B_market_vend{i}_panel", dx - 0.47, 1.15, -2.6, 0.6, 1.2, 0.12,
              (0.05, 0.05, 0.10), emissive=(0.20, 0.85, 1.00), emi_int=0.8)
    # well：中央广场（石圈 + 双柱 + 顶棚 + 灯笼）
    _cyl("B_well_ring", 0, 0.25, 0, 0.9, 0.25, 0.9, (0.55, 0.55, 0.60), tex="stone.png")
    _cube("B_well_post1", -0.55, 0.80, 0, 0.12, 1.1, 0.12, (0.30, 0.24, 0.18))
    _cube("B_well_post2", 0.55, 0.80, 0, 0.12, 1.1, 0.12, (0.30, 0.24, 0.18))
    _cube("B_well_roof", 0, 1.42, 0, 1.6, 0.14, 1.6, (0.24, 0.26, 0.20), tex="roof_dark.png")
    _sphere("B_well_lantern", 0, 1.05, 0, 0.18, (0.9, 0.7, 0.35),
            emissive=(1.0, 0.78, 0.35), emi_int=1.3)
    _tag("B_well_ring")


# ---------------------------------------------------------------- 街道设施
def build_street():
    _plane("Road", 0, 0.01, 0, 0.5, 3.4, ROAD_RGB, tex="asphalt.png")
    _plane("Sidewalk_W", -3.5, 0.02, 0, 0.2, 3.4, SIDEWALK_RGB, tex="sidewalk.png")
    _plane("Sidewalk_E", 3.5, 0.02, 0, 0.2, 3.4, SIDEWALK_RGB, tex="sidewalk.png")

    for side, x in (("W", -2.9), ("E", 2.9)):
        for i, z in enumerate((-7.5, 0, 7.5)):
            p = f"Lamp_{side}{i}"
            _cyl(f"{p}_pole", x, 2.0, z, 0.12, 2.0, 0.12, (0.16, 0.17, 0.20))
            _cube(f"{p}_head", x, 4.18, z, 0.55, 0.2, 0.55, (0.9, 0.85, 0.6),
                  emissive=LAMP_WARM, emi_int=0.8)
            _light(f"{p}_light", x, 3.9, z, LAMP_WARM, intensity=1.0)

    for side, x in (("W", -3.5), ("E", 3.5)):
        _cube(f"Bench_{side}", x, 0.35, 4.3, 2.0, 0.5, 0.7, (0.30, 0.22, 0.15), tex="planks.png")
        _cube(f"Bench_{side}_back", x, 0.85, 4.3 - 0.33, 2.0, 0.55, 0.12, (0.30, 0.22, 0.15))

    for z, rgb in ((-10.8, (0.95, 0.30, 0.75)), (10.8, (0.25, 0.85, 1.00))):
        s = "S" if z < 0 else "N"
        _cube(f"Gate_{s}_beam", 0, 3.6, z, 6.2, 0.6, 0.3, (0.08, 0.08, 0.12),
              emissive=rgb, emi_int=1.2)
        _cyl(f"Gate_{s}_p1", -3.0, 1.9, z, 0.15, 1.9, 0.15, (0.40, 0.42, 0.48), tex="concrete.png")
        _cyl(f"Gate_{s}_p2", 3.0, 1.9, z, 0.15, 1.9, 0.15, (0.40, 0.42, 0.48), tex="concrete.png")

    for i in range(4):
        _plane(f"Cross_zebra{i}", 0, 0.012, 2.2 + i * 1.2, 0.5, 0.12, (0.75, 0.77, 0.82))


def build():
    print("[搭建] 等待 Unity REST Server 就绪...")
    health = us.wait_for_health(timeout=120)
    if not health:
        print("[错误] 未检测到 Unity Server，请先在 Unity 里启动 UnitySkills Server。")
        return
    print(f"[搭建] Unity 已就绪：{health.get('unityVersion') or health}")

    # 0) 清理：旧村庄方块 + 上一轮街道部件（只删本脚本命名对象，绝不碰 NPC_*）
    for b in OLD_VILLAGE:
        _delete(b)
    for n in _own_names():
        _delete(n)
    print("[清理] 旧村庄与上轮街道部件已清理")

    # 1) 地面与街道
    _plane("Ground", 0, 0, 0, 2.6, 3.4, ROAD_RGB, tex="asphalt.png")
    build_street()
    print("[搭建] 街道/人行道/路灯/长椅/贩卖机/门头 完成")

    # 2) 建筑
    for bid, zh, x, z, f, body, btex, awn, atex, sign, stex, win in BUILDINGS:
        build_facade(bid, zh, x, z, f, body, btex, awn, atex, sign, stex, win)
        print(f"[搭建] {zh} @ ({x},{z})")
    build_specials()
    print("[搭建] 7 栋建筑 + 特色道具 完成")

    # 3) 保存
    if not ARGS.no_save:
        _call("scene_save", scenePath=SCENE_PATH)
        print(f"[搭建] 场景已保存到 {SCENE_PATH}")
    else:
        print("[搭建] 按 --no-save 未保存，可在 Unity 里手动 Ctrl+S")

    print("\n✅ 六分街场景搭建完成（贴图版）！")
    print("   建筑已贴砖墙/木板/石材/沥青贴图，招牌是中文霓虹字。")
    print("   你的 NPC / BridgeHub / Camera 均保持原样；建筑已自动挂 PerceptTag。")
    print("   运行 .\\venv\\Scripts\\python.exe -m src.bridge 后，NPC 会主动逛这条街。")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true", help="施工但不保存场景")
    ARGS = ap.parse_args()
    build()
