# -*- coding: utf-8 -*-
"""
意识层关联图性能基准 + 零偏差校验（A优化：相似度/match 缓存）。

用法（在 src/clayer 目录下，用 venv 的 python 运行）：
  python bench_assoc.py verify        # 跑 off/on 两子进程，断言激活结果 FINGERPRINT 完全一致（零偏差）
  python bench_assoc.py on|off        # 单次跑"节点递增耗时曲线"，展示越聊越慢被缓解的程度
"""
import os
import sys
import time
import random
import subprocess

MODE = sys.argv[1] if len(sys.argv) > 1 else "on"
# 必须在 import assoc_graph 之前设好开关（模块顶部读一次）
if MODE == "off":
    os.environ["PYCLAYER_DISABLE_SIM_CACHE"] = "1"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from assoc_graph import AssocGraph          # noqa: E402
import cl_config as config                  # noqa: E402


class MockMem:
    """最小 MemoryStore 接口替身，让 edge_weight/spread 真正扩散（count>0）。"""
    def count(self, c):
        return 5
    def recent_info_increase(self, c):
        return 0.0
    def recency(self, c):
        return 0.0


def build_graph(n_nodes: int, avg_degree: int):
    g = AssocGraph()
    words = [f"w{i}" for i in range(n_nodes)]
    random.seed(42)
    for i in range(n_nodes):
        k = random.randint(1, avg_degree)
        targets = random.sample(words, min(k, n_nodes))
        g.link_group([words[i]] + targets)
    g.finalize_strength(words)
    return g, words


def fingerprint(act):
    return round(sum(round(v, 6) for v in act.values()), 6)


def run_curve():
    mem = MockMem()
    print(f"=== MODE={MODE} (cache {'OFF=原版' if MODE=='off' else 'ON=A优化'}) ===")
    sizes = [200, 500, 1000, 1500, 2000]
    fp = None
    for N in sizes:
        g, words = build_graph(N, 15)
        rounds = 60
        t0 = time.perf_counter()
        last = None
        for r in range(rounds):
            seeds = {w: 1.0 for w in random.sample(words, 8)}
            last = g.spread_activation(seeds, mem)
        dt = time.perf_counter() - t0
        if fp is None and last is not None:
            fp = fingerprint(last)
        print(f"  N={N:5d}  avg/spread={dt/rounds*1000:8.2f}ms  total={dt*1000:9.1f}ms  activated={len(last)}")
    print(f"  FINGERPRINT={fp}")
    return fp


def verify():
    py = sys.executable
    script = os.path.abspath(__file__)
    r_off = subprocess.run([py, script, "off"], capture_output=True, text=True)
    r_on = subprocess.run([py, script, "on"], capture_output=True, text=True)
    fp_off = _grep_fp(r_off.stdout)
    fp_on = _grep_fp(r_on.stdout)
    print("--- off stdout ---")
    print(r_off.stdout)
    print("--- on stdout ---")
    print(r_on.stdout)
    if fp_off is None or fp_on is None:
        print("VERIFY FAIL: 无法解析 fingerprint")
        sys.exit(1)
    if abs(fp_off - fp_on) < 1e-6:
        print(f"VERIFY PASS: 激活结果完全一致 (off={fp_off} == on={fp_on})，缓存零偏差。")
    else:
        print(f"VERIFY FAIL: 偏差 off={fp_off} on={fp_on}")
        sys.exit(1)


def _grep_fp(text: str):
    for line in text.splitlines():
        if line.startswith("  FINGERPRINT="):
            return float(line.split("=")[1])
    return None


if __name__ == "__main__":
    if MODE == "verify":
        verify()
    else:
        run_curve()
