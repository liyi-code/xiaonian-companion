# -*- coding: utf-8 -*-
"""
从 cl_config.py 自动生成 cpp_config.h。

设计目的（通用性 / 防漂移）：
  意识层的核心常量(衰减系数、强度标定、扩散跳数、阈值…)是"人类神经意识科学"
  模型的关键参数。Python 版(cl_config.py)是单一事实源；C++ 版不能手抄一份，
  否则两边迟早漂移、破坏"功能不变"。

  本脚本在每次编译前运行，把 cl_config.py 里的数值常量原样 emit 成
  cpp_config.h 的 #define，确保 C++ 与 Python 用完全一致的参数。

仅抽取"被 C++ 核心算法使用"的常量（其余如 WELLBEING 词库、LLM 地址等由 Python 侧处理）。
"""
import os
import sys

# 让脚本能 import 同目录上一级的 cl_config
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import cl_config as cfg  # noqa: E402

# C++ 核心算法实际使用的常量名（与 assoc_graph.py / memory_store.py 中 import 的保持一致）
NAMES = [
    # memory_store
    "STORAGE_CAPACITY", "REINFORCE_NODE", "DECAY_PER_TURN", "RECENCY_TAU",
    # assoc_graph - 关联/强度
    "REINFORCE_EDGE", "PROXIMITY_WINDOW", "PROXIMITY_DECAY",
    "EDGE_CAPACITY_PER_NODE",
    "STRENGTH_K", "STRENGTH_REF", "STRENGTH_ASSOC_MIN", "SIM_THRESHOLD",
    "COMPOSE_BONUS",
    # assoc_graph - 强度近因衰减
    "STRENGTH_RECENCY_ENABLED", "STRENGTH_RECENCY_DECAY",
    "STRENGTH_IDLE_GRACE", "STRENGTH_FORGET_THRESHOLD",
    # assoc_graph - 解锁优先级
    "INFO_W", "STRENGTH_W", "RECENCY_W", "INFO_DELTA_REF",
    # assoc_graph - 匹配度门控
    "MATCH_THRESHOLD", "MATCH_ASSOC_W",
    # assoc_graph - 扩散激活
    "SPREAD_HOPS", "SPREAD_DECAY", "SPREAD_THRESHOLD", "SPREAD_MAX_UNLOCK",
    # assoc_graph - 压缩整合 / 睡眠
    "SLEEP_COST_K", "CONSOLIDATE_STRENGTH_MAX", "CONSOLIDATE_BATCH",
]


def fmt(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        # 保证写成合法 double 字面量
        s = repr(v)
        if "." not in s and "e" not in s and "E" not in s:
            s += ".0"
        return s
    return None


def main():
    out_path = os.path.join(_HERE, "cpp_config.h")
    lines = [
        "// AUTO-GENERATED from cl_config.py by gen_cpp_config.py -- DO NOT EDIT BY HAND",
        "#pragma once",
        "",
    ]
    missing = []
    for name in NAMES:
        if not hasattr(cfg, name):
            missing.append(name)
            continue
        v = getattr(cfg, name)
        s = fmt(v)
        if s is None:
            # 非数值常量（如集合/字符串）C++ 侧暂不需要，跳过
            continue
        lines.append(f"#define {name} {s}")
    if missing:
        sys.stderr.write(f"[gen_cpp_config] WARNING: 缺失常量 {missing}\n")
    lines.append("")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[gen_cpp_config] OK -> {out_path} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
