# -*- coding: utf-8 -*-
"""
意识 orchestrator —— 整个"上层框架"的心脏。

一次完整的意识活动 think(text)：
  1) 感知   perceive : 文本 -> 概念(信息单元)，作为种子
  2) 激活   activate : 已知概念按统计显著度给更大初始能量
  3) 链式解锁 unlock  : 沿关联图扩散激活，解锁一串相关概念(带衰减/阈值)
  4) 概率   probability: 权重(激活*统计)经 softmax -> 概率；统计量调制温度；
                         基础/最大概率钳制(弱项有机会/强项不独裁)
  5) 组合   combine  : 按概率伪随机无放回采样 K 个概念 = "这一念"的内容
  6) 组装   assemble : 把这一念 + 采样倾向(温度/top_p) 交给 LLM 后端
学习 learn(...)：把这次对话回写(统计++、共现建边、衰减遗忘、容量淘汰)，
                 于是这套"意识"会随使用长出自己的倾向。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Tuple
import concurrent.futures as _cf
import json
import math
import os
import threading
import time

import cl_config as config
import perception
import _core
from _core import (
    AssocGraph,
    MemoryStore,
    ProbabilityEngine,
    normalized_entropy,
)
from affect import AffectState, Emotion
# 双通路睡眠引擎（依赖注入，弱引用避免循环导入）
from sleep import DualPathwaySleep
# 主动找话题引擎（从睡眠成果挑种子，纯规则不调 LLM）
from topic import TopicEngine

# 启动自检：C++ 后端与 Python 基准数值一致才启用；不一致/异常时 _core 自动回退 Python。
# 仅首次实例化 Consciousness 时跑一次，避免重复开销。
_SELF_TESTED = False

def _ensure_clayer_self_test() -> None:
    global _SELF_TESTED
    if not _SELF_TESTED:
        _SELF_TESTED = True
        try:
            # 延迟导入：避开 token_bias->consciousness->_core 的循环导入
            from _core import self_test
            self_test()
        except Exception:
            pass


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, t))


@dataclass
class Thought:
    """一道并行浮现的"意念/意向"。"""
    concepts: List[Tuple[str, float]] = field(default_factory=list)  # 该念的概念->概率
    event_strength: float = 0.0       # 事件强度(变量，词越多越强)
    emotion: Emotion = field(default_factory=Emotion)  # 该念的情绪(效价-唤醒)
    wellbeing: float = 0.0            # 价值分[-1,1]：契合"让使用者生活越来越好"的程度
    attention: float = 0.0            # 竞争胜出的注意力份额[0,1]，和为1 -> 资源分配
    vigor: float = 0.0                # 竞争力(决定注意力)
    is_primary: bool = False          # 是否主念(注意力最高)

    def line(self) -> str:
        return "、".join(f"{c}" for c, _ in self.concepts)


@dataclass
class ConsciousState:
    """一次意识活动的完整快照，供 LLM 后端与调试使用。"""
    seeds: List[str] = field(default_factory=list)
    unlocked: Dict[str, float] = field(default_factory=dict)      # 链式解锁的概念->能量
    distribution: Dict[str, float] = field(default_factory=dict)  # 最终选取概率
    chosen: List[Tuple[str, float]] = field(default_factory=list) # 主念概念(对外兼容下游)
    thoughts: List[Thought] = field(default_factory=list)         # 并行竞争的多道念
    temperature: float = config.TEMP_BASE                         # 统计调制后的意识温度
    entropy: float = 0.0                                          # 归一化熵：0专注~1发散
    llm_temperature: float = 0.7
    llm_top_p: float = 0.9
    bias_entries: int = 0                                         # token级介入的 logit_bias 条目数
    event_strength: float = 0.0                                   # 主念事件强度(对外兼容下游)
    mood: Emotion = field(default_factory=Emotion)               # 全局情绪基调快照
    # ---------- 睡眠 / 压缩整合（性能触发） ----------
    sleep_signal: str = ""        # "" | "sleepy_hint"(犯困) | "forced_sleep"(强制睡眠)
    sleep_state: str = "awake"    # "awake" | "sleepy" | "forced"
    traverse_ms: float = 0.0      # 本轮回合遍历记忆图的耗时(ms)
    # ---------- 想象力出口（通路二合成概念回查） ----------
    imagined: List[Tuple[str, float]] = field(default_factory=list)  # [(组合key, 得分), ...]

    def primary(self) -> "Thought":
        if not self.thoughts:
            return Thought()
        return max(self.thoughts, key=lambda t: t.attention)

    def thought_line(self) -> str:
        return self.primary().line()

    def debug(self) -> str:
        top = sorted(self.distribution.items(), key=lambda kv: kv[1], reverse=True)[:8]
        parts = [f"{c}:{p:.2f}" for c, p in top]
        ordered = sorted(self.thoughts, key=lambda t: t.attention, reverse=True)
        tl = []
        for i, th in enumerate(ordered):
            emo = th.emotion.label() if th.emotion else "?"
            tl.append(f"念{i+1}[{'主' if th.is_primary else '副'}|{emo}|注意{th.attention:.2f}|强{th.event_strength:.1f}|善{th.wellbeing:+.2f}]={th.line()}")
        mood_s = f"情绪({self.mood.valence:+.2f},{self.mood.arousal:.2f})"
        return (f"[意识] 种子={self.seeds[:6]} 解锁{len(self.unlocked)}个 "
                f"温度={self.temperature:.2f} 熵={self.entropy:.2f} "
                f"事件强度={self.event_strength:.2f} {mood_s} "
                f"\n    选念=[{self.thought_line()}] 分布Top={parts} "
                f"\n    多念={' | '.join(tl)} "
                f"\n    -> LLM(temp={self.llm_temperature:.2f},top_p={self.llm_top_p:.2f},"
                f"token偏置={self.bias_entries}条)")


class Consciousness:
    def __init__(self,
                 mem: MemoryStore | None = None,
                 graph: AssocGraph | None = None,
                 prng_seed=config.PRNG_SEED):
        self.mem = mem or MemoryStore()
        self.graph = graph or AssocGraph()
        self.prob = ProbabilityEngine(seed=prng_seed)
        self.affect = AffectState()
        # ---------- 睡眠 / 压缩整合（性能触发） ----------
        self.traverse_ms: float = 0.0     # 本轮回合遍历记忆图的耗时(ms)
        self.think_ms: float = 0.0        # think 内部计算的耗时(ms)
        self.sleep_state: str = "awake"   # "awake" | "sleepy" | "forced"
        self.sleep_count: int = 0         # 累计进入强制睡眠(压缩整合)次数
        self.user_screening_needed: bool = False  # 压缩后仍超阈值 -> 需用户筛选存储
        # 线程安全：think(读) / learn(写) / consolidate / save 共享 mem+graph，
        # 后台并行 learn 与 think 可能并发，加 RLock 保证互斥一致。
        self._lock = threading.RLock()
        # 启动自检：校验 C++ 后端一致（不一致自动回退），保证"算得快且算得对"
        _ensure_clayer_self_test()
        # 后端是否 C++（pybind11 调用释放 GIL -> think 内多念可真并行；
        # Python 回退后端 GIL 不释放，多念并行会负优化，故改为串行）。
        self._cpp = getattr(_core, "CPP_AVAILABLE", False)
        # 挂起的异步学习线程（learn_async 启动）；think/consolidate 开始前先 join，
        # 保证 mem/graph 不被「后台写」与「think 读」并发（一致性契约，免加全局锁）。
        self._learn_thread: "threading.Thread | None" = None
        # ---------- 双通路睡眠引擎（通路二：创新组合，模拟想象力） ----------
        # 依赖注入：sleep.DualPathwaySleep 持弱引用回指本意识层，不反向导入。
        self.sleep_engine = DualPathwaySleep()
        self.sleep_engine.set_owner(self)
        # ---------- 主动找话题引擎（从睡眠成果挑种子，纯规则不调 LLM） ----------
        self.topic_engine = TopicEngine()
        self.topic_engine.set_owner(self)

    # ---------- 主流程 ----------
    def think(self, text: str) -> ConsciousState:
        # 等挂起的异步 learn 完成，避免与后台写 mem/graph 并发（一致性契约）
        self._join_pending_learn()
        st = ConsciousState()
        # 1) 感知（带显著度权重 + 词序）
        weighted = perception.extract_weighted(text)
        seeds = [c for c, _ in weighted]
        st.seeds = seeds
        if not seeds:
            # 表层感知不出概念（卡壳）→ 想象力出口：回查高权重合成概念索引
            st.imagined = self._recall_imagination([])
            return st

        # 2) 激活：初始能量 = 感知显著度(重要词更用力) + 该概念的历史统计显著度
        #    显著词一进来就带更高能量，链式解锁会更倾向从它们出发。
        seed_energy: Dict[str, float] = {}
        for c, w in weighted:
            base = w if config.SEED_ENERGY_FROM_WEIGHT else 1.0
            seed_energy[c] = base + self.mem.salience(c)

        # 3~6) 链式解锁 / 概率 / 多念竞争 / 组装，整段计时（"遍历记忆图"的核心耗时）
        t0 = time.perf_counter()
        activation = self.graph.spread_activation(seed_energy, self.mem)
        st.unlocked = activation

        # 4) 权重 -> 概率（统计调制温度 + 基础/最大概率钳制）
        salience = {c: self.mem.salience(c) for c in activation}
        probs, temp = self.prob.build_distribution(
            activation, salience, self.mem.total_observations()
        )
        st.distribution = probs
        st.temperature = temp

        # 熵 -> 专注/发散
        nent = normalized_entropy(probs)
        st.entropy = nent

        # 5) 多念竞争：从分布里抽至多 MAX_THOUGHTS 道"对比鲜明"的候选念(逐代抑制)
        seed_set = set(seeds)
        candidates = self._generate_thoughts(probs, nent)

        # 5.5) 每道念算 事件强度 / 情绪 / 价值分。
        #      C++ 后端(pybind11)调用释放 GIL -> 多线程真并行；Python 回退后端 GIL 不释放，
        #      多线程会负优化，故改串行。念数<=1 时也无并行必要。
        if candidates:
            if self._cpp and len(candidates) > 1:
                with _cf.ThreadPoolExecutor(
                        max_workers=min(len(candidates), config.MAX_THOUGHTS)) as ex:
                    list(ex.map(lambda th: self._fill_thought(th, nent), candidates))
            else:
                for th in candidates:
                    self._fill_thought(th, nent)

        # 5.7) 竞争：竞争力 vigor(强度+匹配+唤醒+价值) -> 注意力分配(softmax) -> 标记主念
        self._compete(candidates, seed_set)
        st.thoughts = candidates

        # 5.8) 想象力出口：扩散激活思索不出候选念（表层卡壳）时，回查通路二合成概念索引
        if not candidates:
            st.imagined = self._recall_imagination(seeds)

        # 主念(注意力最高)对外暴露为 chosen/event_strength，保持下游(token_bias/learn)兼容
        if candidates:
            prim = max(candidates, key=lambda t: t.attention)
            st.chosen = prim.concepts
            st.event_strength = prim.event_strength
            prim.is_primary = True
            # 全局情绪向主念缓慢漂移，形成连续情绪流
            self.affect.update(st.seeds, prim.emotion)
        st.mood = Emotion(self.affect.valence, self.affect.arousal)

        # 6) 采样倾向：熵映射到 LLM 温度/top_p（发散->更高温）
        st.llm_temperature = _lerp(config.LLM_TEMP_MIN, config.LLM_TEMP_MAX, nent)
        st.llm_top_p = _lerp(config.LLM_TOPP_MIN, config.LLM_TOPP_MAX, nent)
        self.think_ms = (time.perf_counter() - t0) * 1000.0

        # ---------- 遍历耗时 = think 内部耗时 + 记忆图 O(N) 维护代价估算 ----------
        # 维护代价随节点/边数增长，是"越聊越慢"的真正来源；用它驱动睡眠机制。
        self.traverse_ms = self.think_ms + self.graph.benchmark_ms()
        self._evaluate_sleep(st)
        return st

    # ---------- 自发动作：把动画状态也注册成概念，由环境上下文驱动 ----------
    def register_actions(self, actions: List[str]) -> None:
        """把动作意图码注册为概念节点（如 [ACT_SIT]），先给基础强度方便早期扩散。"""
        with self._lock:
            for a in actions:
                if not a:
                    continue
                # 直接以当前基础强度插入节点（setdefault 不会覆盖已有值）。
                # 注意：AssocGraph 不存 activations（那是 spread_activation 的返回值），
                # 只需初始化 strength + edges 并 touch 刷新近因时间戳。
                # C++ 后端(stl.h 拷贝语义)不支持对返回 dict 的就地 setdefault 写回，
                # 故走显式方法 ensure_strength / ensure_edge_slot（Python 后端同名方法兼容）。
                self.graph.ensure_strength(a, config.BASE_STRENGTH)
                self.graph.ensure_edge_slot(a)
                self.graph.touch(a)

    def spontaneous_action(
        self,
        context: List[str],
        action_prefix: str = "[ACT_",
    ) -> Tuple[str | None, float]:
        """
        根据环境上下文（时间/地点/玩家状态等概念种子）做快速扩散激活，
        返回概率最高的动作意图码（如 [ACT_SIT]）及其概率。不调用 LLM，纯图计算。
        """
        seeds = [c for c in context if c]
        if not seeds:
            return None, 0.0
        self._join_pending_learn()
        with self._lock:
            seed_energy = {c: 1.0 + self.mem.salience(c) for c in seeds}
            activation = self.graph.spread_activation(seed_energy, self.mem)
            salience = {c: self.mem.salience(c) for c in activation}
            probs, _ = self.prob.build_distribution(
                activation, salience, self.mem.total_observations()
            )
            action_probs = {c: p for c, p in probs.items() if c.startswith(action_prefix)}
            if not action_probs:
                return None, 0.0
            best = max(action_probs.items(), key=lambda kv: kv[1])
            # 刷新相关概念近因时间戳（高频动作自然不被遗忘）
            self.graph.touch_many([best[0]] + seeds)
            return best[0], float(best[1])

    def reinforce_action(
        self,
        action: str,
        context: List[str],
        success: bool = True,
        amount: float | None = None,
    ) -> None:
        """
        动作执行反馈：成功则强化 context↔action 的边；失败则弱化，避免对着空气重复执行。
        同时刷新 last_active，让高频习惯动作留在记忆里。
        """
        if not action or not context:
            return
        amt = amount if amount is not None else config.REINFORCE_EDGE
        self._join_pending_learn()
        with self._lock:
            self.graph.touch(action)
            self.graph.touch_many(context)
            for c in context:
                if not c or c == action:
                    continue
                if success:
                    self.graph.link(c, action, amt)
                else:
                    self.graph.weaken(c, action, amt * 0.5)

    # ---------- 想象力出口（表层卡壳 → 回查通路二合成概念索引） ----------
    def _recall_imagination(self, seeds: List[str]) -> List[Tuple[str, float]]:
        """
        表层扩散激活/感知思索不出答案时的"想象力出口"：调用通路二合成的概念索引，
        尝试用睡眠时碰撞出的联想来解决问题。返回 [(组合key, 得分), ...]。
        由 DUAL_PATHWAY_ENABLED 开关控制；关闭/无索引时返回空列表，不影响主流程。
        """
        if not config.DUAL_PATHWAY_ENABLED:
            return []
        try:
            return self.sleep_engine.retrieve_creative(seeds or [])
        except Exception:
            return []

    # ---------- 主动找话题（Idle Hook：从睡眠成果挑种子，纯规则不调 LLM） ----------
    def idle_topic(self) -> dict | None:
        """
        主动找话题的 Idle Hook：由上层（gui 的 _proactive_tick，30 秒轮询）在满足三条件
        （①空闲≥5分钟 ②状态非勿扰/专注 ③距上次≥2小时）时调用。
        内部纯规则：从来源A（创新组合）/ 来源B（遗忘预警）取权重最高种子，套模板话术。
        绝不调用 LLM（用户回复后才启动 LLM 续聊）；无可用种子返回 None。
        返回 {source, seed_key, seed, other, text}。
        """
        if not config.TOPIC_ENABLED:
            return None
        try:
            return self.topic_engine.pick_topic()
        except Exception:
            return None

    def topic_feedback(self, delta: float) -> None:
        """用户对小念主动话题的反馈（正=加权，负=拉黑+缩短间隔）。"""
        try:
            self.topic_engine.feedback(delta)
        except Exception:
            pass

    # ---------- 睡眠状态机（性能触发） ----------
    def _evaluate_sleep(self, st: ConsciousState) -> None:
        """
        根据遍历耗时判睡眠态，并写入 state 供下游(LLM/UI)使用：
          · >= SLEEP_FORCE_MS  -> 强制睡眠(forced_sleep)：下游应停输出 + 压缩整合
          · >= SLEEP_WARN_MS    -> 犯困(sleepy_hint)：下游注入"想睡"引导，仍正常对话
          · <  SLEEP_RECOVER_MS -> 解除睡眠态，回到 awake
        """
        ms = self.traverse_ms
        st.traverse_ms = ms
        st.sleep_state = self.sleep_state
        if ms >= config.SLEEP_FORCE_MS:
            self.sleep_state = "forced"
            st.sleep_signal = "forced_sleep"
        elif ms >= config.SLEEP_WARN_MS:
            self.sleep_state = "sleepy"
            st.sleep_signal = "sleepy_hint"
        else:
            if self.sleep_state in ("sleepy", "forced") and ms < config.SLEEP_RECOVER_MS:
                self.sleep_state = "awake"
            st.sleep_signal = ""

    def force_sleep(self) -> dict:
        """用户手动强制睡眠：立即进入强制态并压缩整合记忆。返回报告。"""
        self.sleep_state = "forced"
        return self.consolidate_memory()

    def sleep_cycle(self) -> dict:
        """
        双通路睡眠：睡眠时并行两种记忆处理。
          通路一：consolidate_memory() —— 语义压缩备份（弱词并入强词，降遍历耗时）。
          通路二：sleep_engine.innovate_combine() —— 创新组合备份（高权重节点随机组合成
                  合成概念，打新权重=成员权重和，存独立索引，模拟想象力/创造力雏形）。
        通路二由 DUAL_PATHWAY_ENABLED 开关控制，关闭时行为与旧版完全一致（可一键回退）。
        返回合并报告。
        """
        report = self.consolidate_memory()
        if config.DUAL_PATHWAY_ENABLED:
            created = self.sleep_engine.innovate_combine()
            report["combo_created"] = created
            report["combo_total"] = len(self.sleep_engine.combos)
            report["pathway2_enabled"] = True
        else:
            report["combo_created"] = 0
            report["pathway2_enabled"] = False
        return report

    def consolidate_memory(self) -> dict:
        """
        压缩整合记忆（应对遍历耗时过高）。目标由强度决定：把『强度快要归零』的弱词并入其
        最强相关词，循环直到遍历耗时回落到阈值以下、或无更多可合并的弱词为止。
        整合后若耗时仍超阈值 -> 置 user_screening_needed=True（交给用户筛选存储）。
        返回报告 dict。
        """
        self._join_pending_learn()   # 与后台 learn 互斥，避免并发写 mem/graph
        before_nodes = self.graph.node_count()
        before_ms = self.think_ms + self.graph.benchmark_ms()
        steps = 0
        while steps < config.SLEEP_CONSOLIDATE_MAX_STEPS:
            cur = self.think_ms + self.graph.benchmark_ms()
            if cur < config.SLEEP_FORCE_MS:
                break
            merged = self.graph.consolidate()
            if not merged:
                break
            # 同步清理记忆库里被合并的词（图与记忆库保持一致）
            for c, _ in merged:
                self.mem.pop_count(c)
                self.mem.pop_last_seen(c)
            steps += 1
        after_nodes = self.graph.node_count()
        after_ms = self.think_ms + self.graph.benchmark_ms()
        self.traverse_ms = after_ms
        if after_ms >= config.SLEEP_FORCE_MS:
            self.user_screening_needed = True
            self.sleep_state = "forced"
        else:
            self.user_screening_needed = False
            self.sleep_state = "awake"
        self.sleep_count += 1
        return {
            "before_nodes": before_nodes,
            "after_nodes": after_nodes,
            "before_ms": round(before_ms, 2),
            "after_ms": round(after_ms, 2),
            "steps": steps,
            "sleep_count": self.sleep_count,
            "screening_needed": self.user_screening_needed,
        }

    # ---------- 用户筛选存储模式（压缩后仍超阈值时的兜底） ----------
    def backup_graph(self, path: str | None = None) -> str:
        """备份另存整个词库(关联图)到独立 JSON（筛选存储选项①）。"""
        os.makedirs(config.SLEEP_BACKUP_DIR, exist_ok=True)
        if not path:
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(config.SLEEP_BACKUP_DIR, f"mind_backup_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.graph.to_dict(), f, ensure_ascii=False)
        return path

    def delete_all_memory(self) -> None:
        """删除整个词库（筛选存储选项②：清空重来）。"""
        self.graph = AssocGraph()
        self.mem = MemoryStore()
        self.affect = AffectState()
        self.traverse_ms = 0.0
        self.think_ms = 0.0
        self.sleep_state = "awake"
        self.user_screening_needed = False

    def export_filtered(self, keep: set, path: str | None = None) -> str:
        """
        筛选另存（筛选存储选项③）：只保留 keep 集合里的概念及其内部关联，其余删除；
        记忆库同步裁剪。返回导出路径。
        """
        os.makedirs(config.SLEEP_EXPORT_DIR, exist_ok=True)
        if not path:
            ts = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(config.SLEEP_EXPORT_DIR, f"mind_filtered_{ts}.json")
        new_graph = AssocGraph.from_dict(self.graph.to_dict())
        for c in list(new_graph.snapshot_strength().keys()):
            if c not in keep:
                new_graph.drop(c)
        new_counts = {c: v for c, v in self.mem.get_counts().items() if c in keep}
        self.graph = new_graph
        self.mem.set_counts(new_counts)
        self.traverse_ms = self.think_ms + self.graph.benchmark_ms()
        self.user_screening_needed = False
        self.sleep_state = "awake"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.graph.to_dict(), f, ensure_ascii=False)
        return path

    def list_concepts(self, n: int | None = None) -> List[Tuple[str, float]]:
        """列出概念(按强度降序)，供用户筛选 UI。返回 [(概念, 强度), ...]。"""
        items = sorted(self.graph.snapshot_strength().items(), key=lambda kv: kv[1], reverse=True)
        return items[:n] if n else items

    def screening_status(self) -> dict:
        return {
            "screening_needed": self.user_screening_needed,
            "sleep_state": self.sleep_state,
            "traverse_ms": round(self.traverse_ms, 2),
            "nodes": self.graph.node_count(),
        }

    # ---------- 多念生成（逐代抑制，保证各念对比鲜明） ----------
    def _generate_thoughts(self, probs: Dict[str, float], nent: float) -> List[Thought]:
        """
        从同一分布里抽至多 MAX_THOUGHTS 道候选念：抽完一道后，把其概念大幅降权，
        再对剩余分布抽下一道 -> 各念主题对比鲜明、互不雷同。
        """
        k = self.prob.pick_k(len(probs), focus=1.0 - nent)
        candidates: List[Thought] = []
        work = dict(probs)
        for _ in range(config.MAX_THOUGHTS):
            if not work or sum(work.values()) <= 0:
                break
            drawn = self.prob.combine(work, k)
            if len(drawn) < 1:
                break
            candidates.append(Thought(concepts=drawn))
            # 抑制已抽概念，留给下一念"不同的声音"
            for c, _ in drawn:
                work[c] = work.get(c, 0.0) * (1.0 - config.THOUGHT_SUPPRESS)
            z = sum(work.values()) or 1.0
            work = {c: p / z for c, p in work.items()}
        return candidates

    # ---------- 价值分：让使用者的生活越来越好 ----------
    def _thought_wellbeing(self, concepts: List[Tuple[str, float]]) -> float:
        """
        价值分[-1,1]：该念契合"让使用者生活越来越好"元目标的程度。
        命中正向价值词库 +1，命中负向 -1，最后按概念数归一化（避免多词念天然虚高）。
        这是意识层最底层的"判断标准"——即便某念事件更强/更躁动，若它在拉低生活品质，
        价值分会把它整体压下去；反之助益生活的念会被托起，成为主念。

        匹配用【子串容错】：无 jieba 时概念被切成单字/2字 ngram（如"学""熬夜"），
        用完整词库("学习""熬夜")做 == 比对会全部 miss、价值分恒为0。
        故 c 与词库词 w 任一方向子串包含即算命中，鲁棒于分词粗细。
        """
        if not config.WELLBEING_ENABLED or not concepts:
            return 0.0
        score = 0.0
        for c, _ in concepts:
            if self._well_match(c, config.WELLBEING_POSITIVE):
                score += 1.0
            elif self._well_match(c, config.WELLBEING_NEGATIVE):
                score -= 1.0
        return max(-1.0, min(1.0, score / len(concepts)))

    @staticmethod
    def _well_match(c: str, lexicon) -> bool:
        if c in lexicon:
            return True
        for w in lexicon:
            if w in c or c in w:   # 双向子串包含：鲁棒于分词过细/过粗
                return True
        return False

    # ---------- 多念竞争（vigor -> 注意力） ----------
    def _compete(self, candidates: List[Thought], seed_set: set) -> None:
        """
        每道念的竞争力 vigor = 事件强度(饱和 frac) + 与种子匹配度 + 情绪唤醒度 + 价值分。
        注意力 = softmax(vigor / ATTENTION_TEMP)，和为1 -> 资源分配；最高者即主念。
        价值分作为第四轴，使输出天然偏向"让使用者生活越来越好"的方向。
        vigor 计算（含 match_degree 读图，C++ 释放 GIL）可并行；attention 依赖全体 vigor，串行。
        """
        if candidates:
            if self._cpp and len(candidates) > 1:
                with _cf.ThreadPoolExecutor(
                        max_workers=min(len(candidates), config.MAX_THOUGHTS)) as ex:
                    list(ex.map(lambda th: self._fill_vigor(th, seed_set), candidates))
            else:
                for th in candidates:
                    self._fill_vigor(th, seed_set)
        atts = self.affect.allocate_attention([th.vigor for th in candidates])
        for th, a in zip(candidates, atts):
            th.attention = a

    def _fill_vigor(self, th: "Thought", seed_set: set) -> None:
        """单道念的竞争力 vigor：与种子最佳匹配度 + 强度饱和 + 唤醒 + 价值。"""
        best_match = 0.0
        for c, _ in th.concepts:
            m = self.graph.match_degree(c, seed_set)
            if m > best_match:
                best_match = m
        frac = th.event_strength / (th.event_strength + config.EVENT_STRENGTH_REF)
        th.vigor = (config.VIGOR_STRENGTH_W * frac
                    + config.VIGOR_MATCH_W * best_match
                    + config.VIGOR_AROUSAL_W * th.emotion.arousal
                    + config.WELLBEING_W * th.wellbeing)

    def _fill_thought(self, th: "Thought", nent: float) -> None:
        """单道念的派生量：事件强度 + 情绪 + 价值分（只读，线程安全）。"""
        th.event_strength = self.graph.event_strength([c for c, _ in th.concepts])
        th.emotion = self.affect.thought_emotion(th.concepts, nent, th.event_strength)
        th.wellbeing = self._thought_wellbeing(th.concepts)

    # ---------- 学习回写 ----------
    def learn(self, user_text: str, response_text: str, state: ConsciousState,
              user_concepts: List[str] | None = None,
              chosen_concepts: List[str] | None = None) -> None:
        """
        统计++（脑容量里记牢）、共现建边（长关联链）、衰减遗忘、容量淘汰。
        参与共现的概念 = 用户输入 + 这一念选中的 + 回复里出现的。
        动态逐段模式下可显式传入 user_concepts(原始用户输入)与 chosen_concepts(各段主念并集)，
        避免把"模型自己刚说的话"误当作用户输入。
        """
        user_concepts = user_concepts if user_concepts is not None \
            else (state.seeds or perception.extract(user_text))
        resp_concepts = perception.extract(response_text)
        chosen_concepts = chosen_concepts if chosen_concepts is not None \
            else [c for c, _ in state.chosen]
        all_concepts = list(dict.fromkeys(user_concepts + resp_concepts + chosen_concepts))

        # 快照：本轮回填前先记下来，事后算"信息增加量"
        prev_counts = self.mem.snapshot_counts()
        prev_words = set(self.mem.counts.keys())

        # 统计：所有相关概念计数++
        self.mem.observe_many(user_concepts)
        self.mem.observe_many(resp_concepts)
        self.mem.observe_many(chosen_concepts, amount=config.REINFORCE_NODE * 0.5)

        # 共现建边（粒度升级）：
        #  - 句内按"邻近"建链，保留词序结构（相邻词关联最强）
        self.graph.link_sequence(user_concepts)
        self.graph.link_sequence(resp_concepts)
        #  - 再把"用户输入 / 这一念 / 回复"三束跨接起来（袋式弱连），绑定问-念-答
        bridge = list(dict.fromkeys(user_concepts + chosen_concepts + resp_concepts))
        self.graph.link_group(bridge, amount=config.REINFORCE_EDGE * config.PROXIMITY_DECAY)

        # 新词定型强度(单向)：本轮回填前未出现的概念 = 新词，按"关联数+相似数"定其强度；
        # 与之关联的旧词强度不变 -> 满足"永远只影响新词"。
        new_words = [c for c in all_concepts if c not in prev_words]
        self.graph.finalize_strength(new_words)

        # 回填"信息增加量"（喂下一轮解锁优先级"信息量增加"项）
        self.mem.commit_deltas(prev_counts)

        # 衰减遗忘 + 容量淘汰 + 连带清边
        evicted = self.mem.tick()
        self.graph.decay()   # 推进逻辑时钟 turn += 1
        for c in evicted:
            self.graph.drop(c)
        # 关联图全局节点封顶（双保险：防图无限膨胀/意大利面）
        self.graph.enforce_node_cap(config.GRAPH_NODE_CAPACITY)
        # 标记本轮回填/使用的概念为「刚被调取」（近因强度衰减用：刚用过的词有宽限期不会衰减）
        self.graph.touch_many(all_concepts)
        # 近因强度衰减：长期没被调用的概念强度逐轮降，低于阈值即遗忘（解决冷门概念永驻、思维越积越乱）
        self.graph.decay_strength_by_recency()

    # ---------- 异步学习（与 TTS 播放重叠，不阻塞对话回合） ----------
    def _join_pending_learn(self) -> None:
        """一致性契约：任何读/写 mem/graph 的操作前，先等挂起的异步 learn 完成。"""
        lt = self._learn_thread
        if lt is not None and lt.is_alive():
            lt.join()
        self._learn_thread = None

    def learn_async(self, *args, **kwargs) -> None:
        """
        后台异步学习回写（含 save）：与 TTS 播放重叠，对话回合可立即返回。
        线程句柄存入 self._learn_thread，think/consolidate 开头会先 join 它，
        保证 mem/graph 不被并发读写。
        """
        t = threading.Thread(target=self._learn_sync, args=args, kwargs=kwargs, daemon=True)
        self._learn_thread = t
        t.start()

    def _learn_sync(self, *args, **kwargs) -> None:
        """learn_async 的后台目标：同步跑 learn + 持久化，异常静默。"""
        try:
            self.learn(*args, **kwargs)
            self.save()
        except Exception:
            pass

    # ---------- 持久化：储存量随使用长期积累 ----------
    def save(self, path: str = config.STATE_FILE) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "memory": self.mem.to_dict(),
            "graph": self.graph.to_dict(),
            "affect": self.affect.to_dict(),
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
        # 通路二合成概念索引独立持久化（不影响主 mind.json 的 C++/Python parity）
        try:
            self.sleep_engine.save()
        except Exception:
            pass

    @classmethod
    def load(cls, path: str = config.STATE_FILE, prng_seed=config.PRNG_SEED) -> "Consciousness":
        if not os.path.exists(path):
            return cls(prng_seed=prng_seed)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            mem = MemoryStore.from_dict(data.get("memory", {}))
            graph = AssocGraph.from_dict(data.get("graph", {}))
            obj = cls(mem=mem, graph=graph, prng_seed=prng_seed)
            obj.affect = AffectState.from_dict(data.get("affect", {}))
            obj.sleep_engine.load()
            return obj
        except Exception:
            return cls(prng_seed=prng_seed)

    # ---------- 供 LLM 使用：把"这一念"组装成引导文本 ----------
    def compose_guidance(self, state: ConsciousState) -> str:
        """
        把意识层并行浮现的多道念组装成给 LLM 的"潜意识联想"提示。
        主念(注意力最高)主导语言倾向；副念作为潜意识的微妙底色。情绪基调一并注入。
        """
        if not state.thoughts:
            # 表层卡壳：无常规意念，但有想象力出口的合成概念时，把它们作为"灵感"交给 LLM
            if state.imagined:
                combos = "、".join(k.replace("⋈", " + ") for k, _ in state.imagined[:3])
                return (
                    f"【潜意识联想场】表层一时想不到直接答案，但睡眠时碰撞出的灵感浮现了："
                    f"{combos}。请尝试把这几组联想组合起来，用一个新颖的角度回应。"
                    f"\n【底层价值导向】本意识以\"让使用者的生活越来越好\"为元目标，"
                    f"请在回应中延续这份向善的倾向。"
                )
            return ""
        ordered = sorted(state.thoughts, key=lambda t: t.attention, reverse=True)
        primary = ordered[0]
        # 展示过滤：无 jieba 时概念多为单字 ngram，仅展示 >=2 字片段，降低引导噪声
        # （价值匹配/竞争仍用全部概念，此处只影响"给人看/给模型看的文字"）。
        def _disp(th):
            items = [c for c, _ in th.concepts if len(c) >= 2]
            return "、".join(items) if items else th.line()
        lines = []
        for i, th in enumerate(ordered):
            tag = "主念" if i == 0 else f"副念{i}"
            emo = th.emotion.label() if th.emotion else "中性"
            total_p = sum(p for _, p in th.concepts)
            lines.append(f"  - [{tag}|{emo}|注意{th.attention:.2f}|善{th.wellbeing:+.2f}] {_disp(th)}（联想强度 {total_p:.2f}）")
        mood_label = primary.emotion.label() if primary.emotion else "中性"
        focus = "发散跳跃" if state.entropy > 0.66 else ("平衡" if state.entropy > 0.33 else "专注聚焦")
        return (
            f"【潜意识联想场】当前意识状态：{focus}，情绪{mood_label}。"
            f"此刻并行浮现的意念（最多 {config.MAX_THOUGHTS} 道）：\n"
            + "\n".join(lines)
            + "\n请以主念为主导，让回答自然带上这层联想倾向；副念作为潜意识的微妙底色，"
              "不要生硬罗列它们。"
            + "\n【底层价值导向】本意识以\"让使用者的生活越来越好\"为元目标，"
              "主念已优先契合助益生活的方向——请在回应中延续这份向善的倾向，"
              "温和地引导对方走向更积极、健康、有秩序的状态。"
        )
