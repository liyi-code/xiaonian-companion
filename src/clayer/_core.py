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
import os
import sys
from typing import Any

CPP_AVAILABLE = False
CPP_REASON = ""
CPP_SOURCE = "python(fallback)"

try:
    # pyclayer.pyd 与 _core.py 同目录（src/clayer/）。若运行时该目录不在 sys.path，
    # 顶层 import pyclayer 会失败；这里确保它能找到（幂等）。
    _clayer_dir = os.path.dirname(os.path.abspath(__file__))
    if _clayer_dir not in sys.path:
        sys.path.insert(0, _clayer_dir)
    import pyclayer  # 编译产物：pyclayer.pyd (Win) / pyclayer*.so (Linux/mac)
    if not (hasattr(pyclayer, "AssocGraph") and hasattr(pyclayer, "MemoryStore")):
        CPP_REASON = "pyclayer 缺少必要类"
    else:
        # C++ 扩展必须暴露上层 consciousness.py 直接调用的方法接口，
        # 否则 stl.h 拷贝语义下这些字段访问不会写回 C++ 对象，导致意识层失效。
        _g_proto = pyclayer.AssocGraph()
        _m_proto = pyclayer.MemoryStore()
        _need_methods = {
            "AssocGraph": ("ensure_strength", "ensure_edge_slot", "snapshot_strength"),
            "MemoryStore": ("get_counts", "set_counts", "get_last_seen",
                            "set_last_seen", "pop_count", "pop_last_seen"),
        }
        _missing = []
        for _cls_name, _methods in _need_methods.items():
            _obj = _g_proto if _cls_name == "AssocGraph" else _m_proto
            for _m in _methods:
                if not hasattr(_obj, _m):
                    _missing.append(f"{_cls_name}.{_m}")
        if _missing:
            CPP_REASON = "pyclayer C++ API 不完整，缺少: " + ", ".join(_missing)
        else:
            CPP_AVAILABLE = True
            CPP_SOURCE = f"pyclayer(C++ v{getattr(pyclayer, 'cpp_version', '?')})"
except Exception as e:  # 没编译 / 缺编译器 / ABI 不匹配 等
    CPP_REASON = str(e)

if CPP_AVAILABLE:
    AssocGraph = pyclayer.AssocGraph           # type: ignore
    MemoryStore = pyclayer.MemoryStore         # type: ignore
else:
    # clayer 子模块使用裸导入（如 import cl_config），需要把本目录加入 sys.path
    _clayer_dir = os.path.dirname(os.path.abspath(__file__))
    if _clayer_dir not in sys.path:
        sys.path.insert(0, _clayer_dir)
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
        _clayer_dir = os.path.dirname(os.path.abspath(__file__))
        if _clayer_dir not in sys.path:
            sys.path.insert(0, _clayer_dir)
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
        _clayer_dir = os.path.dirname(os.path.abspath(__file__))
        if _clayer_dir not in sys.path:
            sys.path.insert(0, _clayer_dir)
        from assoc_graph import AssocGraph as _PyG
        from memory_store import MemoryStore as _PyM
        AssocGraph = _PyG
        MemoryStore = _PyM
        if verbose:
            print("[clayer] C++ 与 Python 基准不一致，已禁用 C++ 回退 Python。")
    return ok
