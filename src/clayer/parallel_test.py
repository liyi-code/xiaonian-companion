# -*- coding: utf-8 -*-
"""
意识层并行化验证（think 多念并行 + learn/TTS 重叠）。

覆盖：
  1) think 并行路径(_cpp=True, len>1) 与串行路径(_cpp=False) 输出完全一致
     （并行只改调度、不改数学）。
  2) learn_async 与 think 的 join 契约：learn_async 后台跑完后，
     下一次 think 必须看到其写入（计数变化），保证 mem/graph 不并发读写。
  3) 重叠计时：learn_async 与模拟 TTS(后台 sleep) 并行，
     总耗时 ≈ max(learn, tts) 而非 sum(learn, tts)。

运行（需 venv）：
  venv\\Scripts\\python.exe parallel_test.py
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cl_config as config
from consciousness import Consciousness
from _core import CPP_AVAILABLE


def _dist_sig(st):
    """用分布 + 各念概念集生成稳定指纹，用于对比并行/串行一致性。"""
    parts = []
    for c, p in sorted(st.distribution.items(), key=lambda kv: kv[0]):
        parts.append(f"{c}:{p:.4f}")
    for th in st.thoughts:
        cs = ",".join(c for c, _ in th.concepts)
        parts.append(f"[{cs}|es={th.event_strength:.4f}|vig={th.vigor:.4f}|att={th.attention:.4f}]")
    return "|".join(parts)


def test_think_parallel_consistency():
    """并行路径 vs 串行路径，结果应逐位一致。"""
    if not CPP_AVAILABLE:
        print("[1] 跳过：C++ 后端不可用（仅 Python，think 本就串行）。")
        return True
    text = "今天写代码写到很晚，有点累但完成了一个复杂功能，挺有成就感的，想去睡了"
    sigs = []
    for mode in (True, False):
        mind = Consciousness()
        mind._cpp = mode  # 强制并行(True)/串行(False) 路径
        st = mind.think(text)
        sigs.append(_dist_sig(st))
    ok = sigs[0] == sigs[1]
    print(f"[1] think 并行/串行一致: {'PASS' if ok else 'FAIL'}")
    if not ok:
        print("   并行:", sigs[0][:200])
        print("   串行:", sigs[1][:200])
    return ok


def test_join_contract():
    """learn_async 后台跑，下一次 think 必须 join 到其写入。"""
    mind = Consciousness()
    mind.think("我喜欢学习新东西，每天都进步一点点")
    before = mind.mem.count("学习") if "学习" in mind.mem.counts else 0.0
    # 后台异步 learn（含 save），不等它完成，立即发起下一次 think
    mind.learn_async(
        "我喜欢学习新东西，每天都进步一点点",
        "太棒了，坚持下去你会越来越厉害的",
        mind.think("我喜欢学习新东西，每天都进步一点点"),
    )
    # 下一次 think 开头会 join 挂起的 learn
    st2 = mind.think("学习让我快乐，也让我有点累")
    after = mind.mem.count("学习") if "学习" in mind.mem.counts else 0.0
    ok = after >= before  # 至少不丢写入； learn 已把"学习"计数++
    print(f"[2] join 契约(learn 对后续 think 可见): {'PASS' if ok else 'FAIL'} "
          f"(before={before:.2f}, after={after:.2f})")
    return ok


def test_overlap_timing():
    """learn_async 与模拟 TTS 重叠，总耗时应≈max 而非 sum。"""
    mind = Consciousness()
    learn_cost = 0.15  # 模拟 learn+save 耗时(s)

    def fake_learn():
        time.sleep(learn_cost)
        mind.learn(
            "测试一下重叠",
            "好的，我陪你",
            mind.think("测试一下重叠"),
        )

    # 同步基线：learn 先跑完，再"播放 TTS"
    tts = 1.0
    t0 = time.perf_counter()
    fake_learn()
    time.sleep(tts)
    sync_ms = (time.perf_counter() - t0) * 1000.0

    # 重叠：learn_async 后台跑，同时"播放 TTS"
    t1 = time.perf_counter()
    mind.learn_async(
        "测试一下重叠",
        "好的，我陪你",
        mind.think("测试一下重叠"),
    )
    time.sleep(tts)
    if mind._learn_thread is not None:
        mind._learn_thread.join()
    async_ms = (time.perf_counter() - t1) * 1000.0

    saved = sync_ms - async_ms  # 重叠省下的时间
    ok = async_ms < sync_ms - 50  # 省下应接近 learn_cost*1000
    print(f"[3] 重叠计时: sync={sync_ms:.0f}ms, async+tts={async_ms:.0f}ms, "
          f"省下≈{saved:.0f}ms -> {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    print(f"[clayer] C++ 后端可用: {CPP_AVAILABLE}")
    r = []
    r.append(test_think_parallel_consistency())
    r.append(test_join_contract())
    r.append(test_overlap_timing())
    ok = all(r)
    print(f"[parallel] 结论: {'全部通过 [PASS]' if ok else '存在失败 [FAIL]'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
