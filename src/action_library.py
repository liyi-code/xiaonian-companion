# -*- coding: utf-8 -*-
"""
小念动作资产库（Action Library）——"动作 × prompt"可持续学习平台的地基。

数据模型（data/action_library.json）：
  actions: [
    { "id": "act_xxx", "name": "开心挥手",
      "clip": "Assets/Animations/act_xxx.anim",   # Unity 动画资源路径（VRChat 阶段可换 emote 名）
      "duration": 2.1,
      "profile": {"energy": 0.7, "speed": 0.8, "upper_body": 0.9, "periodicity": 0.7},
      "tags": ["wave", "joy"],
      "prompts": [                                  # 触发条件（可多个，强度独立）
        { "text": "你开心的时候", "meta": {"type":"emotion","dim":"joy","th":0.5},
          "strength": 0.82, "hits": 14, "last_used": "..." } ],
      "source": "mocap|unity_taught", "learned_at": "...",
    }, ...
  ]

学习动力学（全部参数可调）：
  · 教学绑定：add_prompt，strength 起始 0.5
  · 使用强化：record_use → strength 向 1.0 靠拢（hits+1）
  · 反馈驯化：record_feedback(+1夸奖 / 0沉默 / -1嫌弃)
      +1 → strength 向 1.0；-1 → 立即降到阈值以下（等于忘记这条 prompt）；
      0（沉默/无视）→ 缓慢衰减（"看脸色知错就改"）
  · 自然遗忘：decay_all 定期调用，长期不用 strength 向 0 衰减，跌破 forget_th 自动移入
      forgotten（保留记录，可被重新教学）
  · 创新候选：pending 动作（无 prompt 的新入库动作）供 LLM 提议"这个动作什么时候用？"

线程安全 + 原子写（os.replace），与 clayer/custom_skills 同风格。
"""
import os
import re
import json
import sys
import time
import threading
import uuid
from datetime import datetime

# 让 src 目录可被 import（无论从哪启动，与 bridge.py 同法）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from config import CONFIG

_PATH = os.path.join(CONFIG["data_dir"], "action_library.json")
_HOT_DIR = os.path.join(CONFIG["data_dir"], "action_inbox")   # 动捕/动画文件热文件夹
_LOCK = threading.RLock()

# 学习动力学参数
STRENGTH_INIT = 0.5          # 教学绑定的初始强度
USE_GAIN = 0.08              # 每次成功运用：向 1 靠拢的步长
PRAISE_GAIN = 0.12           # 夸奖反馈
SILENCE_DECAY = 0.03         # 沉默/无视：单次衰减
FORGET_TH = 0.15             # 低于此强度 = 遗忘（移入 forgotten）
DECAY_HALF_LIFE = 14 * 86400 # 自然遗忘半衰期（14 天；每天衰减约 5%）

# 运动学特征（从动捕/动画统计得出，供 LLM 提议 prompt 时参考）
PROFILE_KEYS = ("energy", "speed", "upper_body", "periodicity")


def _load():
    try:
        with open(_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("actions"), list):
            return d
    except Exception:
        pass
    return {"actions": [], "forgotten": []}


def _save(data):
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _PATH)   # 原子写，防半截文件损坏


def _now():
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 动作 CRUD
# --------------------------------------------------------------------------- #
def add_action(name, clip, duration=0.0, profile=None, tags=None, source="mocap"):
    """注册一个动作资产。返回 action_id。"""
    with _LOCK:
        data = _load()
        aid = "act_" + uuid.uuid4().hex[:8]
        action = {
            "id": aid,
            "name": (name or f"动作{len(data['actions']) + 1}").strip()[:40],
            "clip": (clip or "").strip(),
            "duration": max(0.0, float(duration or 0)),
            "profile": {k: float((profile or {}).get(k, 0.0)) for k in PROFILE_KEYS},
            "tags": [str(t)[:24] for t in (tags or [])][:16],
            "prompts": [],
            "source": source,
            "learned_at": _now(),
        }
        data["actions"].append(action)
        _save(data)
        return aid


def add_prompt(action_id, text, meta=None, strength=STRENGTH_INIT):
    """给动作绑定一条触发条件（教学）。返回 (ok, msg)。"""
    text = (text or "").strip()
    if not text:
        return False, "触发条件不能为空"
    with _LOCK:
        data = _load()
        for a in data["actions"]:
            if a["id"] == action_id:
                for p in a["prompts"]:
                    if p["text"] == text:
                        return False, f"这个动作已经有「{text}」这条条件了"
                a["prompts"].append({
                    "text": text[:60],
                    "meta": meta if isinstance(meta, dict) else None,
                    "strength": float(strength),
                    "hits": 0,
                    "last_used": _now(),
                    "created": _now(),
                })
                _save(data)
                return True, f"学会啦：{a['name']} ← 「{text}」"
        return False, f"找不到动作 {action_id}"


def find_action(name_or_id):
    with _LOCK:
        for a in _load()["actions"]:
            if a["id"] == name_or_id or a["name"] == name_or_id:
                return a
    return None


def list_actions():
    with _LOCK:
        return list(_load()["actions"])


def pending_actions():
    """新入库、还没有任何 prompt 的动作（供 LLM 主动提议"什么时候用"）。"""
    return [a for a in list_actions() if not a["prompts"]]


# --------------------------------------------------------------------------- #
# 学习动力学：使用强化 / 反馈驯化 / 自然遗忘
# --------------------------------------------------------------------------- #
def _bump(v, step):
    return min(1.0, max(0.0, v + step * (1.0 - v) if step > 0 else v + step))


def record_use(action_id, prompt_text, success=True):
    """运用一次：成功则强化。返回当前 strength。"""
    with _LOCK:
        data = _load()
        for a in data["actions"]:
            if a["id"] == action_id:
                for p in a["prompts"]:
                    if p["text"] == prompt_text:
                        if success:
                            p["strength"] = _bump(p["strength"], USE_GAIN)
                            p["hits"] = int(p.get("hits", 0)) + 1
                        p["last_used"] = _now()
                        _save(data)
                        return p["strength"]
    return None


def record_feedback(action_id, prompt_text, sentiment):
    """玩家反馈：+1 夸奖 / 0 沉默无视 / -1 嫌弃。返回 (strength, 状态)。"""
    with _LOCK:
        data = _load()
        for a in data["actions"]:
            if a["id"] == action_id:
                for p in a["prompts"]:
                    if p["text"] == prompt_text:
                        if sentiment >= 1:
                            p["strength"] = _bump(p["strength"], PRAISE_GAIN)
                        elif sentiment <= -1:
                            p["strength"] = 0.0   # 嫌弃：立即遗忘（"知错就改"）
                        else:
                            p["strength"] = _bump(p["strength"], -SILENCE_DECAY)
                        p["last_used"] = _now()
                        _save(data)
                        return p["strength"], "active"
    return None, None


def decay_all(now_ts=None):
    """自然遗忘：按半衰期衰减所有 prompt；跌破阈值移入 forgotten。
    建议由睡眠流程（clayer consolidate）每日调用一次。"""
    now_ts = now_ts or time.time()
    with _LOCK:
        data = _load()
        forgotten = data.get("forgotten", [])
        moved = 0
        for a in data["actions"]:
            alive = []
            for p in a["prompts"]:
                try:
                    last = datetime.fromisoformat(p.get("last_used", p.get("created", _now()))).timestamp()
                except Exception:
                    last = now_ts
                age = max(0.0, now_ts - last)
                decay = 0.5 ** (age / DECAY_HALF_LIFE)
                p["strength"] = round(float(p.get("strength", 0.5)) * decay, 4)
                if p["strength"] < FORGET_TH:
                    forgotten.append({"action": a["id"], "prompt": p["text"],
                                      "forgot_at": _now()})
                    moved += 1
                else:
                    alive.append(p)
            a["prompts"] = alive
        data["forgotten"] = forgotten[-500:]
        _save(data)
        return moved


# --------------------------------------------------------------------------- #
# 热文件夹：动捕文件入库（新动作"进水口"）
# --------------------------------------------------------------------------- #
SUPPORTED_EXTS = (".anim", ".fbx", ".bvh")


def scan_hotfolder():
    """扫描 data/action_inbox/：新文件自动注册为 pending 动作（等待配 prompt）。
    返回新入库数量。"""
    if not os.path.isdir(_HOT_DIR):
        return 0
    with _LOCK:
        data = _load()
        known = {a["clip"] for a in data["actions"]}
        added = 0
        for fn in sorted(os.listdir(_HOT_DIR)):
            if not fn.lower().endswith(SUPPORTED_EXTS):
                continue
            src = os.path.join(_HOT_DIR, fn)
            # clip 路径：Unity 侧约定动捕文件拷入 Assets/Animations/ 后同名使用
            clip = "Assets/Animations/" + fn
            if clip in known:
                continue
            name = os.path.splitext(fn)[0].replace("_", " ").strip() or fn
            add_action(name, clip, source="hotfolder")
            known.add(clip)
            added += 1
        return added


# --------------------------------------------------------------------------- #
# 自检
# --------------------------------------------------------------------------- #
def selftest():
    aid = add_action("自检动作", "Assets/Animations/test.anim", duration=2.0,
                     tags=["test"], source="selftest")
    print(f"[动作库] 新建动作: {aid}")
    ok, msg = add_prompt(aid, "测试触发")
    print(f"[动作库] 绑定 prompt: {ok} :: {msg}")
    s = record_use(aid, "测试触发")
    print(f"[动作库] 运用一次后 strength={s}（应 > 0.5）")
    s, _ = record_feedback(aid, "测试触发", -1)
    print(f"[动作库] 嫌弃后 strength={s}（应为 0.0）")
    ok2, _ = add_prompt(aid, "重新教")
    print(f"[动作库] 重新教学: {ok2}")
    moved = decay_all()
    print(f"[动作库] 自然遗忘一轮（无到期项，moved={moved}）")
    # 清理
    with _LOCK:
        data = _load()
        data["actions"] = [a for a in data["actions"] if a["id"] != aid]
        _save(data)
    print("[动作库] 自检完成（测试数据已清理）")


if __name__ == "__main__":
    selftest()
