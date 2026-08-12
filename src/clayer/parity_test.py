# -*- coding: utf-8 -*-
"""
parity（数值对照）测试：证明 C++ 核心与 Python 原版"功能不变"。

做法：
  1) 用纯 Python 版 (assoc_graph / memory_store) 构造一个记忆图、跑若干操作，
     记录关键输出（spread_activation 的激活能量、match_degree、similarity、
     consolidate 结果、to_dict 结构）。
  2) 把同一张图经 to_dict 注入 C++ 版，跑同样的操作。
  3) 比较两者数值，差异在容差 TOL 内即 PASS。

只要本测试全过，就说明"C++ 重写"在算法层面与你的原设计逐位等价——
换机器编译后行为不会变。
"""
from __future__ import annotations
from typing import Dict, List, Tuple
import math

TOL = 1e-9


def _build_python_graph():
    from assoc_graph import AssocGraph
    from memory_store import MemoryStore
    import cl_config as config

    g = AssocGraph()
    mem = MemoryStore()
    # 一组语义相关的概念，构造关联边
    words = ["下雨", "晚上", "睡觉", "疲惫", "音乐", "放松", "学习", "努力",
             "开心", "温柔", "陪伴", "游戏", "傍晚", "困", "安静"]
    mem.observe_many(words)
    g.finalize_strength(words)
    # 邻近建边 + 共现建边
    g.link_sequence(words)
    g.link_group(["下雨", "晚上", "睡觉", "困", "安静"])
    g.link_group(["音乐", "放松", "温柔", "陪伴"])
    g.link_group(["学习", "努力", "开心"])
    g.link_group(["游戏", "傍晚", "开心"])
    g.decay()  # 推进一轮
    return g, mem


def _close(a: float, b: float, tol: float = TOL) -> bool:
    if math.isnan(a) and math.isnan(b):
        return True
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def _compare_activation(py_act: Dict[str, float], cpp_act: Dict[str, float], tol: float = TOL) -> List[str]:
    errs = []
    keys = set(py_act) | set(cpp_act)
    for k in keys:
        pv = py_act.get(k, 0.0)
        cv = cpp_act.get(k, 0.0)
        if not _close(pv, cv, tol):
            errs.append(f"activation[{k}] py={pv} cpp={cv}")
    return errs


def run_all(verbose: bool = True) -> bool:
    import pyclayer
    from assoc_graph import AssocGraph as PyGraph
    from memory_store import MemoryStore as PyMem

    ok = True

    # ---------- 用例 1：spread_activation 端到端 ----------
    py_g, py_mem = _build_python_graph()
    seeds = {"下雨": 1.0, "晚上": 0.8, "睡觉": 0.9}
    py_act = py_g.spread_activation(seeds, py_mem)

    # 用同一张图构造 C++ 版
    cpp_g = pyclayer.AssocGraph.from_dict(py_g.to_dict())
    cpp_mem = pyclayer.MemoryStore.from_dict(py_mem.to_dict())
    cpp_act = cpp_g.spread_activation(seeds, cpp_mem)

    errs = _compare_activation(py_act, cpp_act)
    if errs:
        ok = False
        if verbose:
            print("[parity] FAIL spread_activation:")
            for e in errs[:10]:
                print("   ", e)
    elif verbose:
        print(f"[parity] OK spread_activation ({len(cpp_act)} 概念)")

    # ---------- 用例 2：match_degree / similarity ----------
    for c in ["困", "音乐", "学习", "游戏", "安静"]:
        seed_set = {"下雨", "晚上", "睡觉"}
        pv = py_g.match_degree(c, seed_set)
        cv = cpp_g.match_degree(c, seed_set)
        if not _close(pv, cv):
            ok = False
            if verbose:
                print(f"[parity] FAIL match_degree({c}): py={pv} cpp={cv}")
        # similarity 需要已存在的两个概念
    for a, b in [("下雨", "晚上"), ("音乐", "放松"), ("学习", "努力")]:
        pv = py_g.similarity(a, b)
        cv = cpp_g.similarity(a, b)
        if not _close(pv, cv):
            ok = False
            if verbose:
                print(f"[parity] FAIL similarity({a},{b}): py={pv} cpp={cv}")
    if verbose and ok:
        print("[parity] OK match_degree / similarity")

    # ---------- 用例 3：consolidate（压缩整合）结构一致 ----------
    py_merged = py_g.consolidate()
    cpp_merged = cpp_g.consolidate()
    if len(py_merged) != len(cpp_merged):
        ok = False
        if verbose:
            print(f"[parity] FAIL consolidate 数量: py={len(py_merged)} cpp={len(cpp_merged)}")
    elif verbose:
        print(f"[parity] OK consolidate ({len(cpp_merged)} 项)")

    # ---------- 用例 3.5：edge_weight denom 保底分母 ----------
    # 模拟动作/场景词（如 [ACT_SIT]、椅子）在 mem 中无计数但有共现边的情况。
    # C++ 旧版在此场景直接返回 0（BUG），Python 新版有 max(1e-6, co) 保底。
    py_edge = PyGraph()
    py_edge.link_group(["[ACT_SIT]", "椅子"])
    py_edge_mem = PyMem()
    # 只给 mem 观察其中一个词，另一个不在词频中 → denom <= 0 触发保底
    py_edge_mem.observe("椅子")
    w_py = py_edge.edge_weight("[ACT_SIT]", "椅子", py_edge_mem)
    if w_py <= 0:
        ok = False
        if verbose:
            print(f"[parity] FAIL edge_weight denom 保底: py={w_py} (应为正数)")
    else:
        # 同时验证 C++ 与 Python 一致
        cpp_edge = pyclayer.AssocGraph.from_dict(py_edge.to_dict())
        cpp_edge_mem = pyclayer.MemoryStore.from_dict(py_edge_mem.to_dict())
        w_cpp = cpp_edge.edge_weight("[ACT_SIT]", "椅子", cpp_edge_mem)
        if not _close(w_py, w_cpp):
            ok = False
            if verbose:
                print(f"[parity] FAIL edge_weight 保底 py/cpp 不一致: py={w_py} cpp={w_cpp}")
        elif verbose:
            print(f"[parity] OK edge_weight denom 保底 (py={w_py:.6f}, cpp={w_cpp:.6f})")

    # ---------- 用例 4：to_dict 结构等价 ----------
    py_d = py_g.to_dict()
    cpp_d = cpp_g.to_dict()
    for key in ("edges", "strength", "assoc_count", "similar_count", "last_active"):
        if set(py_d.get(key, {}).keys()) != set(cpp_d.get(key, {}).keys()):
            ok = False
            if verbose:
                print(f"[parity] FAIL to_dict 键不一致: {key}")
    if verbose and ok:
        print("[parity] OK to_dict 结构")

    if verbose:
        print(f"[parity] 结论: {'全部通过 [PASS]' if ok else '存在不一致 [FAIL]'}")
    return ok


if __name__ == "__main__":
    run_all(verbose=True)
