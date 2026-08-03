# -*- coding: utf-8 -*-
"""
联机多玩家记忆隔离 · 正式可复用模块

把"小念移植到联机游戏"时的记忆隔离抽出为通用组件，解决两类场景：

  · MMO（全服共用 NPC）
      - 公共人格/世界记忆：scope = "{npc_id}|GLOBAL"，全服一份，所有玩家共享；
      - 玩家私人关系：    scope = "{npc_id}|player:{player_id}"，严格隔离。

  · RPG + 弱联机（原神型，NPC 绑房主 world）
      - 房主视角：scope = "{npc_id}|world:{owner_id}"，随房主存档保留（persistent）；
      - 访客视角：scope = "{npc_id}|world:{owner_id}|visitor:{visitor_id}"，
        临时参与、不落盘、离场即回收（ephemeral）。

隔离的核心红线（由本模块强制保证）：
  每个 scope 的 Memory 必须指向【独立物理文件】—— memory.json 与归档
  memory_archive.jsonl 都按 scope 派生，绝不共享，否则跨 session 落盘会互相污染
  （单玩家 memory.py 里 archive_path 默认写死全局文件，多玩家时必串台）。

组件：
  · ScopedMemory(Memory)   —— 归档路径跟随 scope 派生的隔离记忆。
  · MemoryRegistry         —— (scope_key -> Session) 注册表，带【超时 TTL + LRU】回收：
                              - 房主/公共档 persistent=True，永不被回收（除非显式 drop）；
                              - 访客档 ephemeral=True，空闲超时或总量超限（LRU）时自动回收。

依赖：仅依赖 memory.Memory 与现有 config.CONFIG；不改动单玩家 memory.py（向后兼容）。
"""
import os
import time
import threading

from config import CONFIG
from memory import Memory
from assistant import Session


# --------------------------------------------------------------------------- #
# 路径派生：把任意 scope_key 映射为合法文件名，并落在独立子目录，互不串台。
# --------------------------------------------------------------------------- #
def _safe_name(scope_key: str) -> str:
    """scope_key 可能含 | : 等，转成文件系统安全名。"""
    return scope_key.replace("|", "__").replace(":", "_").replace("/", "_")


def _scope_dir() -> str:
    d = os.path.join(CONFIG.get("data_dir", "."), "multiplayer")
    os.makedirs(d, exist_ok=True)
    return d


class ScopedMemory(Memory):
    """归档路径跟随 scope 派生的隔离记忆（多玩家记忆隔离的最小修正版）。

    用法：
        mem = ScopedMemory(scope="npc_paimon|player:alice")
    等价于 Memory()，但 memory.json 与归档 .jsonl 都落在
    data_dir/multiplayer/mem_<scope>.json(.jsonl)，物理上与其他 scope 隔离。
    """

    def __init__(self, scope: str, path: str = None):
        self._scope = scope
        # 先算好派生路径，再交给父类（父类会按传入 path 设 self.path，
        # 但仍会把 archive_path 写死成全局文件，所以我们随后覆盖）。
        if path is None:
            base = os.path.join(_scope_dir(), "mem_" + _safe_name(scope))
            path = base + ".json"
        super().__init__(path=path)
        # 关键：归档也隔离——与 memory.json 同目录、同名但 .jsonl
        base = self.path[:-5] if self.path.endswith(".json") else self.path
        self.archive_path = base + ".jsonl"
        # 重新加载隔离后的归档（父类 __init__ 末尾已 load 了一次全局归档，需纠正）
        self._archive = []
        self._index = {}
        self._df = {}
        self._next_id = 0
        self.load()
        self._rebuild_index()


# --------------------------------------------------------------------------- #
# 注册表：scope_key -> Session，带超时 TTL + LRU 回收
# --------------------------------------------------------------------------- #
class _Entry:
    __slots__ = ("session", "scope", "persistent", "last_used", "ref_count")

    def __init__(self, session, scope, persistent):
        self.session = session
        self.scope = scope
        self.persistent = persistent      # True=房主/公共档，永不超时回收
        self.last_used = time.monotonic()  # LRU 时间戳
        self.ref_count = 0                 # 当前持有引用数（可选统计）


class MemoryRegistry:
    """管理联机场景下的多玩家/多 NPC 记忆隔离。

    scope_key 的语义由调用方约定（见模块顶部 MMO / RPG 说明）。
    默认回收策略：
      - ephemeral 档（访客）：空闲超过 ttl_sec 自动回收（不 save，离场即弃）；
      - 总量超过 max_size 时，按 LRU 淘汰最久未用的 ephemeral 档；
      - persistent 档（房主/公共）：永不超时，仅在显式 drop() 时回收（drop 前 save）。
    """

    def __init__(self, ttl_sec: float = 1800.0, max_size: int = 256,
                 reap_interval: float = 60.0, auto_reap: bool = True):
        self._sessions = {}               # scope_key -> _Entry
        self._lock = threading.RLock()
        self.ttl_sec = ttl_sec
        self.max_size = max_size
        self._reap_interval = reap_interval
        self._reaper = None
        if auto_reap:
            self._start_reaper()

    # ---- 构造 scope 的便捷工厂（语义见模块顶部） -------------------------- #
    @staticmethod
    def scope_mmo_global(npc_id: str) -> str:
        return f"{npc_id}|GLOBAL"

    @staticmethod
    def scope_mmo_player(npc_id: str, player_id: str) -> str:
        return f"{npc_id}|player:{player_id}"

    @staticmethod
    def scope_rpg_host(npc_id: str, owner_id: str) -> str:
        return f"{npc_id}|world:{owner_id}"

    @staticmethod
    def scope_rpg_visitor(npc_id: str, owner_id: str, visitor_id: str) -> str:
        return f"{npc_id}|world:{owner_id}|visitor:{visitor_id}"

    def get(self, scope_key: str, is_owner: bool = False,
            persistent: bool = None) -> Session:
        """取（或创建）一个隔离 Session。

        is_owner      : 传入 Session.is_owner（权限语义，房主/主机 True）。
        persistent    : None 时按 is_owner 推断（房主=True 持久，访客=False 临时）；
                        显式传 True/False 可覆盖。
        """
        if persistent is None:
            persistent = is_owner
        with self._lock:
            e = self._sessions.get(scope_key)
            if e is None:
                mem = ScopedMemory(scope=scope_key)
                e = _Entry(Session(memory=mem, is_owner=is_owner),
                           scope=scope_key, persistent=persistent)
                self._sessions[scope_key] = e
                # 总量超限：先回收一波 ephemeral（LRU）
                self._maybe_evict_locked()
            e.last_used = time.monotonic()
            e.ref_count += 1
            return e.session

    def touch(self, scope_key: str) -> None:
        """外部每次使用该 scope 时调用，刷新 LRU 时间戳（防误回收）。"""
        with self._lock:
            e = self._sessions.get(scope_key)
            if e:
                e.last_used = time.monotonic()

    def drop(self, scope_key: str) -> None:
        """显式回收（如玩家退出世界）。persistent 档回收前先 save 落盘。"""
        with self._lock:
            e = self._sessions.pop(scope_key, None)
            if e is None:
                return
            if e.persistent:
                try:
                    e.session.memory.save()
                except Exception:
                    pass

    def _maybe_evict_locked(self) -> None:
        """在持锁状态下，若 ephemeral 档超 max_size，按 LRU 淘汰最久未用者。"""
        if len(self._sessions) <= self.max_size:
            return
        ephemeral = [e for e in self._sessions.values() if not e.persistent]
        if not ephemeral:
            return
        ephemeral.sort(key=lambda x: x.last_used)
        excess = len(self._sessions) - self.max_size
        for e in ephemeral[:excess]:
            # 访客档不 save（ephemeral，离场即弃）；仅从注册表移除
            self._sessions.pop(e.scope, None)

    def _reap_loop(self) -> None:
        while not self._stop:
            time.sleep(self._reap_interval)
            now = time.monotonic()
            with self._lock:
                expired = [e for e in self._sessions.values()
                           if (not e.persistent) and (now - e.last_used) > self.ttl_sec]
                for e in expired:
                    # 访客档不 save（ephemeral，离场即弃）
                    self._sessions.pop(e.scope, None)

    def _start_reaper(self) -> None:
        self._stop = False
        t = threading.Thread(target=self._reap_loop, daemon=True)
        t.start()
        self._reaper = t

    def stop(self) -> None:
        """停止回收线程（进程退出前可选调用）。"""
        self._stop = True

    def stats(self) -> dict:
        """返回当前注册表快照（调试用）。"""
        with self._lock:
            return {
                "total": len(self._sessions),
                "persistent": sum(1 for e in self._sessions.values() if e.persistent),
                "ephemeral": sum(1 for e in self._sessions.values() if not e.persistent),
                "scopes": sorted(self._sessions.keys()),
            }
