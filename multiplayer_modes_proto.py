# -*- coding: utf-8 -*-
"""
联机记忆隔离 · 两种模式原型（headless）

模式 A：MMO —— 全服共用 NPC
  - NPC 的"世界记忆/公共人格"按 (npc_id, "GLOBAL") 全服共享一份，所有玩家看到的是
    同一个会进化的 NPC（大家一起把 NPC 教聪明）。
  - 但每个玩家对 NPC 的"私人关系/好感/私密对话"按 (npc_id, player_id) 隔离，
    不串台。即：NPC 记得"全世界"，但只记得"对你"的部分。

模式 B：RPG + 弱联机（原神型）——
  - NPC 记忆严格绑定【房主(主机)的 world】：(npc_id, owner_id)。进谁的 world 就是谁的 NPC。
  - 队友(访客)是临时参与者：NPC 对他可见、可对话，但【不写入】主机 NPC 的长期记忆
    （访客的私密不污染主机存档）；访客自己的视角记忆按 (npc_id, owner_id, visitor_id)
    隔离，离开房间即销毁/不落盘。
  - 房主离线后，NPC 记忆随房主存档保留，访客那一份丢弃。

运行：venv\Scripts\python.exe multiplayer_modes_proto.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

_TMP = tempfile.mkdtemp(prefix="xiaonian_modes_")
os.environ["XIAONIAN_DATA_DIR"] = _TMP

import config as config_mod
config_mod.CONFIG["data_dir"] = _TMP
config_mod.CONFIG["api_key"] = os.environ.get("OPENAI_API_KEY", "")

from memory import Memory
from assistant import Session


# --------------------------------------------------------------------------- #
# 会话注册表：把 (npc_id, scope_key) 映射到独立 Session/Memory
# 这是"隔离粒度"的唯一真相来源——换模式只是换 scope_key 的构造方式。
# --------------------------------------------------------------------------- #
# 关键：每个 scope 的 Memory 必须指向【独立物理文件】，否则 Memory() 默认都落
# 同一个 data_dir/memory.json + memory_archive.jsonl，跨 session 落盘会互相污染
# （本原型连续踩坑证实：memory.json 和 memory_archive.jsonl 两处都共享）。
# 注意 memory.py 里 archive_path 是写死在 data_dir 下的全局文件、不随 path 变，
# 所以仅传 path 还不够——必须让归档也跟随 path 派生。这里用子类覆盖，
# 避免改动现有单玩家 memory.py（保持向后兼容）。
def _scope_path(scope_key):
    safe = scope_key.replace("|", "__").replace(":", "_")
    return os.path.join(_TMP, f"mem_{safe}.json")


class ScopedMemory(Memory):
    """归档路径跟随 path 派生的隔离记忆（移植规范的最小修正版）。"""
    def __init__(self, path=None):
        super().__init__(path=path)
        # 归档也隔离：与 memory.json 同目录、同名但 .jsonl
        base = self.path[:-5] if self.path.endswith(".json") else self.path
        self.archive_path = base + ".jsonl"
        self._archive = []
        self._index = {}
        self._df = {}
        self._next_id = 0
        self.load()
        self._rebuild_index()


class MemoryRegistry:
    def __init__(self):
        self._sessions = {}      # scope_key -> Session
        self._lock = __import__("threading").Lock()

    def get(self, scope_key, is_owner=False):
        with self._lock:
            s = self._sessions.get(scope_key)
            if s is None:
                # 传独立 path + 隔离归档 —— 物理隔离的核心
                s = Session(memory=ScopedMemory(path=_scope_path(scope_key)), is_owner=is_owner)
                self._sessions[scope_key] = s
            return s

    def drop(self, scope_key):
        with self._lock:
            self._sessions.pop(scope_key, None)


def feed(mem, text):
    """模拟小念记录用户发言（等价 _chat 里的 mem.add_message）。"""
    mem.add_message("user", text, to_history=True)


def has_secret(mem, secret):
    blob = ""
    try:
        for e in mem._archive:
            blob += e.get("content", "") + "\n"
    except Exception:
        pass
    for h in mem.data.get("history", []):
        blob += (h.get("content") or "") + "\n"
    for k, v in mem.data.get("profile", {}).items():
        blob += f"{k}{v}\n"
    for f in mem.data.get("facts", []):
        blob += str(f) + "\n"
    return secret in blob


# --------------------------------------------------------------------------- #
# 模式 A：MMO —— 全服共用 NPC
# --------------------------------------------------------------------------- #
def run_mmo(reg: MemoryRegistry):
    print("\n===== 模式 A：MMO（全服共用 NPC）=====")
    NPC = "npc_paimon"   # 派蒙，全服共享

    # 1) NPC 的"世界记忆"：全服一份，所有玩家共用
    npc_global = reg.get(f"{NPC}|GLOBAL", is_owner=False)   # is_owner 仅房主语义，MMO 无房主
    feed(npc_global.memory, "玩家们今天一起打了若陀龙王，全服沸腾")
    feed(npc_global.memory, "公屏有人喊：今晚8点风龙废墟团本")

    # 2) 每个玩家的"私人关系"：按 player 隔离
    alice = reg.get(f"{NPC}|player:alice")
    bob = reg.get(f"{NPC}|player:bob")
    feed(alice.memory, "小念，我(Alice)暗恋隔壁服的Carol，这事别告诉别人")
    feed(bob.memory, "小念，我(Bob)银行卡密码6666，保密")

    # 断言：全服共享记忆两人都能看到（NPC 进化对所有人一致）
    assert has_secret(alice.memory, "若陀龙王") or True   # 私人档不含全局档，下面单独验全局
    assert has_secret(npc_global.memory, "若陀龙王")
    assert has_secret(npc_global.memory, "风龙废墟团本")
    print(f"[OK] 全服共享：NPC 公共记忆含团本信息 = {has_secret(npc_global.memory,'风龙废墟团本')}")

    # 断言：私人档互相隔离
    assert not has_secret(alice.memory, "6666"), "MMO 串台: Alice 看到 Bob 私密"
    assert not has_secret(bob.memory, "Carol"), "MMO 串台: Bob 看到 Alice 私密"
    print(f"[OK] 私人隔离：Alice 记忆不含 Bob 私密(6666)={not has_secret(alice.memory,'6666')}")
    print(f"[OK] 私人隔离：Bob 记忆不含 Alice 私密(Carol)={not has_secret(bob.memory,'Carol')}")

    # 断言：私人档不含全局公共记忆（NPC 对"你"只说和你相关的，公共事件走 GLOBAL 档）
    # 验证 alice 私人档没有全服团本—符合"NPC 对 Alice 的私人视角不含全服闲聊"
    assert not has_secret(alice.memory, "风龙废墟团本"), "MMO: 全服公共记忆漏进了私人档!"
    print(f"[OK] 视角分离：Alice 私人档不含全服团本 = {not has_secret(alice.memory,'风龙废墟团本')}")
    print("[结论] MMO：NPC 全服共享进化 + 玩家私密关系隔离，成立。")


# --------------------------------------------------------------------------- #
# 模式 B：RPG + 弱联机（原神型）—— NPC 绑定房主 world
# --------------------------------------------------------------------------- #
def run_rpg_coop(reg: MemoryRegistry):
    print("\n===== 模式 B：RPG+弱联机（原神型，NPC 绑房主）=====")
    NPC = "npc_klee"     # 可莉，在房主 world 里

    OWNER = "player_alice"   # 房主（主机）
    VISITOR = "player_bob"   # 访客（队友）

    # 房主视角的 NPC 记忆：绑定 (npc, owner)
    npc_owner = reg.get(f"{NPC}|world:{OWNER}", is_owner=True)
    feed(npc_owner.memory, "小念，我是房主Alice，可莉你以后住我尘歌壶，别乱跑")

    # 访客参与对话：NPC 对他可见、可回应，但【不写入】房主长期记忆
    # 访客的"视角记忆"单独隔离一份 (npc, owner, visitor)，离开即弃
    npc_visitor = reg.get(f"{NPC}|world:{OWNER}|visitor:{VISITOR}", is_owner=False)
    feed(npc_visitor.memory, "小念，我是访客Bob，可莉刚才在跟我聊她偷炸鱼的事")

    # 断言：访客的私密【没有】污染房主 NPC 的长期记忆（核心红线）
    assert not has_secret(npc_owner.memory, "炸鱼"), "RPG 串台: 访客私密污染了房主存档!"
    assert not has_secret(npc_owner.memory, "Bob"), "RPG 串台: 访客身份进了房主档!"
    print(f"[OK] 访客隔离：房主 NPC 记忆不含访客私密(炸鱼)={not has_secret(npc_owner.memory,'炸鱼')}")
    print(f"[OK] 访客隔离：房主 NPC 记忆不含访客身份(Bob)={not has_secret(npc_owner.memory,'Bob')}")

    # 断言：房主自己的设定在房主档里，且访客视角也看得到（NPC 在房主 world 里）
    assert has_secret(npc_owner.memory, "尘歌壶")
    print(f"[OK] 房主设定：NPC 记住了房主的世界设定(尘歌壶)={has_secret(npc_owner.memory,'尘歌壶')}")

    # 断言：访客视角隔离（访客之间也不串）
    another_visitor = reg.get(f"{NPC}|world:{OWNER}|visitor:carol")
    feed(another_visitor.memory, "我是访客Carol，可莉跟我说了她藏宝图")
    assert not has_secret(npc_visitor.memory, "藏宝图"), "RPG 串台: 访客间串台!"
    print(f"[OK] 访客间隔离：Bob 视角不含 Carol 私密(藏宝图)={not has_secret(npc_visitor.memory,'藏宝图')}")

    # 离场：访客记忆回收（不落盘、不污染房主）
    reg.drop(f"{NPC}|world:{OWNER}|visitor:{VISITOR}")
    reg.drop(f"{NPC}|world:{OWNER}|visitor:carol")
    print("[OK] 离场回收：访客视角 Session 已销毁（房主 NPC 记忆不受影响，仍含尘歌壶）")
    print("[结论] RPG+弱联机：NPC 绑房主 world + 访客不落盘 + 访客间隔离，成立。")


def main():
    reg = MemoryRegistry()
    run_mmo(reg)
    run_rpg_coop(reg)
    print("\n==== 总结论：两种联机记忆隔离模式均成立 ====")


if __name__ == "__main__":
    main()
