# -*- coding: utf-8 -*-
"""
意识层统一入口（通用性 / 跨机器安全）。

设计目标：
  1) 优先使用 C++ 加速库 pyclayer（编译产物 pyclayer.pyd / pyclayer*.so）。
  2) 任何原因（没编译 / 编译器缺失 / ABI 不匹配 / 导入异常）导致 C++ 不可用，
     自动回退到纯 Python 实现（assoc_graph.py / memory_store.py / probability.py /
     token_bias.py），保证"换机器、没编译也能跑"，只是慢一点。
  3) 对外暴露统一的类名与函数，调用方（consciousness.py）无需关心后端是谁。

后端判定在 import 时完成一次；可用 CPP_SOURCE 查看当前后端。
"""
from __future__ import annotations
from typing import Any

CPP_AVAILABLE = False
CPP_REASON = ""
CPP_SOURCE = "python(fallback)"

try:
    import pyclayer  # 编译产物：pyclayer.pyd (Win) / pyclayer*.so (Linux/mac)
    if hasattr(pyclayer, "AssocGraph") and hasattr(pyclayer, "MemoryStore"):
        CPP_AVAILABLE = True
        CPP_SOURCE = f"pyclayer(C++ v{getattr(pyclayer, 'cpp_version', '?')})"
    else:
        CPP_REASON = "pyclayer 缺少必要类"
except Exception as e:  # 没编译 / 缺编译器 / ABI 不匹配 等
    CPP_REASON = str(e)

if CPP_AVAILABLE:
    AssocGraph = pyclayer.AssocGraph           # type: ignore
    MemoryStore = pyclayer.MemoryStore         # type: ignore
else:
    from assoc_graph import AssocGraph          # 纯 Python 实现（永久保留）
    from memory_store import MemoryStore        # 纯 Python 实现（永久保留）

# 概率引擎 / token 级偏置：当前仍用 Python 版（后续可同法移植），统一从本入口导出
from probability import ProbabilityEngine, normalized_entropy
from token_bias import build_logit_bias


def backend() -> str:
    return CPP_SOURCE


def reason() -> str:
    return CPP_REASON


def self_test(verbose: bool = True) -> bool:
    """
    启动自检：若启用了 C++，跑一组内置基准用例，验证 C++ 输出与 Python 基准数值一致
    （容差 1e-9）。发现偏差则自动关闭 C++ 回退 Python，避免"算得快但算错"。
    """
    global CPP_AVAILABLE, CPP_SOURCE, AssocGraph, MemoryStore
    if not CPP_AVAILABLE:
        if verbose:
            print(f"[clayer] 使用纯 Python 后端（{CPP_REASON or '未启用 C++'}），功能不变。")
        return True

    try:
        import parity_test
        ok = parity_test.run_all(verbose=verbose)
    except Exception as e:
        CPP_AVAILABLE = False
        CPP_SOURCE = "python(fallback:parity-exception)"
        from assoc_graph import AssocGraph as _PyG
        from memory_store import MemoryStore as _PyM
        AssocGraph = _PyG
        MemoryStore = _PyM
        if verbose:
            print(f"[clayer] parity 自检异常：{e}，已回退 Python。")
        return False

    if not ok:
        CPP_AVAILABLE = False
        CPP_SOURCE = "python(fallback:parity-mismatch)"
        from assoc_graph import AssocGraph as _PyG
        from memory_store import MemoryStore as _PyM
        AssocGraph = _PyG
        MemoryStore = _PyM
        if verbose:
            print("[clayer] C++ 与 Python 基准不一致，已禁用 C++ 回退 Python。")
    return ok
