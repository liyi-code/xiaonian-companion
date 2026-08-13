# -*- coding: utf-8 -*-
"""双通路睡眠引擎测试：通路二创新组合 + slope_utility + 想象力出口 + 回退开关。"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cl_config as config
# 隔离持久化目录
config.STATE_DIR = tempfile.mkdtemp(prefix="sleep_test_")
config.STATE_FILE = os.path.join(config.STATE_DIR, "mind.json")
config.DUAL_COMBO_FILE = os.path.join(config.STATE_DIR, "combos.json")

from consciousness import Consciousness
from sleep import DualPathwaySleep

print("=== 1) 双通路睡眠：通路二创新组合 ===")
c = Consciousness()
# 直接造一批高权重节点（绕开 learn 需要 state 的复杂度）：
# 通过 link 建边 + 手动确保 strength 值足够高（>= threshold 对应 strength_score 归一化）
words = ["学习", "运动", "健康", "朋友", "家人", "计划", "目标", "阅读", "创造", "思考",
         "陪伴", "沟通", "成长", "专注", "复盘", "鼓励", "自信", "勇气", "温柔", "善良"]
# 两两建边（先不设 strength，让 ensure_strengths 按关联数算出高强度）
for i in range(len(words)):
    for j in range(i + 1, len(words)):
        c.graph.link(words[i], words[j], 2.0)
# 补算强度：此时每个词关联 ~19 个邻居，强度 = K*assoc ≈ 19 -> strength_score 达 1.0
c.graph.ensure_strengths()
# 打印确认
sample_score = c.graph.strength_score(words[0])
print(f"   样例 {words[0]} 的 strength_score = {sample_score:.3f} (阈值 {config.DUAL_COMBINE_THRESHOLD})")

high = c.sleep_engine._high_weight_nodes(config.DUAL_COMBINE_THRESHOLD)
print(f"   高权重节点数(>= {config.DUAL_COMBINE_THRESHOLD}): {len(high)}")

report = c.sleep_cycle()
print(f"   睡眠报告: {report}")
assert "combo_created" in report, "报告缺 combo_created"
assert report["pathway2_enabled"] is True
print(f"   通路二生成合成概念数: {report.get('combo_created')}")
print(f"   合成概念总数: {report.get('combo_total')}")

# 再次睡眠应还能继续组合（只要还有高权重节点）
report2 = c.sleep_cycle()
print(f"   第二次睡眠: combo_created={report2.get('combo_created')} total={report2.get('combo_total')}")

print("\n=== 2) 合成概念权重 = 成员权重之和 ===")
if c.sleep_engine.combos:
    for key, cb in list(c.sleep_engine.combos.items())[:3]:
        s = sum(c.graph.strength_score(m) for m in cb.members)
        print(f"   {key[:30]} weight={cb.weight:.3f} 成员权重和={s:.3f}")
        assert abs(cb.weight - s) < 1e-6, "组合权重 != 成员权重之和"

print("\n=== 3) slope_utility 边际效用反馈 ===")
if c.sleep_engine.combos:
    key = next(iter(c.sleep_engine.combos))
    # 记录 6 次正反馈
    for d in [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
        c.sleep_engine.record_feedback(key, d)
    cb = c.sleep_engine.combos[key]
    print(f"   反馈后 slope_utility={cb.slope_utility:.3f} (期望≈0.75)")
    assert abs(cb.slope_utility - 0.75) < 0.01, "slope_utility 移动平均错误"
    # 负反馈应拉低
    c.sleep_engine.record_feedback(key, -1.0)
    print(f"   负反馈后 slope_utility={cb.slope_utility:.3f}")

print("\n=== 4) 想象力出口：表层卡壳回查 ===")
# 找一个合成概念的成员作为种子，应能召回
if c.sleep_engine.combos:
    sample_key, sample_cb = next(iter(c.sleep_engine.combos.items()))
    seed = [sample_cb.members[0]]
    # 先给它正效用，让它通过检索阈值
    for d in [0.8, 0.9]:
        c.sleep_engine.record_feedback(sample_key, d)
    recalled = c.sleep_engine.retrieve_creative(seed, k=3)
    print(f"   种子={seed} 召回={recalled}")
    # 无种子（完全卡壳）也能召回（走空种子分支）
    recalled2 = c._recall_imagination([])
    print(f"   空种子召回数={len(recalled2)}")

print("\n=== 5) 持久化往返 ===")
c.save()
c2 = Consciousness.load(config.STATE_FILE)
print(f"   重新加载后合成概念数: {len(c2.sleep_engine.combos)}")
assert len(c2.sleep_engine.combos) == len(c.sleep_engine.combos), "持久化丢失合成概念"

print("\n=== 6) 回退开关：关闭通路二 ===")
config.DUAL_PATHWAY_ENABLED = False
r = c.sleep_cycle()
print(f"   关闭后睡眠报告: combo_created={r.get('combo_created')} pathway2={r.get('pathway2_enabled')}")
assert r["pathway2_enabled"] is False
assert c._recall_imagination([]) == []
print("   关闭后想象力出口返回空（行为退回旧版）✓")

config.DUAL_PATHWAY_ENABLED = True
print("\nALL SLEEP TESTS PASSED")
