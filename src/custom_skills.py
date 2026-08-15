# -*- coding: utf-8 -*-
"""小念的动态自定义行为库（self-made skills）。

让小念能【自己学会处理一类新请求】，而不用每次靠开发者加代码：

- 当用户提出一个“可模板化”的需求时，小念可调用 manage_skill 定义一个
  自定义行为：{ name, trigger, reply }——命中 trigger 就说 reply。
- 纯文本映射，不执行任何代码、不改源码、不动系统，天然安全。
- 持久化到 data/custom_skills.json（个人数据，被 .gitignore 忽略）。

安全边界（与自主权限一致的红线）：
  1) 只存「触发词 → 回应文本」，不做任意代码执行；
  2) name/trigger 有格式校验（长度、字符、去重）；
  3) 小念可查/加/删，但删除有审计；不触碰系统设置 / .env / 用户文件。
"""

import os
import json
import re
import threading
from datetime import datetime

from config import CONFIG

_MAX_NAME = 24
_MAX_TRIGGER = 40
_MAX_REPLY = 300
_MAX_SKILLS = 60

# --------------------------------------------------------------------------- #
# VRChat 动作库（OSC 桥扩展）：她"能学"的动作必须在这里注册。
# kind=gesture  -> VRChat 官方 8 手势（GestureLeft/Right 参数值 0-7）
# kind=expression -> 化身自定义表情参数（ExprJoy/ExprAngry/...，值 0~1）
# kind=emote     -> 化身预置 emote 名（需化身 Animator 里有同名状态）
# --------------------------------------------------------------------------- #
GESTURE_LIBRARY = {
    0: "自然", 1: "握拳", 2: "摊手", 3: "指人",
    4: "胜利✌️", 5: "摇滚🤘", 6: "手枪👉", 7: "点赞👍",
}
EXPRESSION_ALLOWED = {
    "joy": "开心", "angry": "生气", "sad": "难过",
    "calm": "平静", "anxious": "不安", "surprised": "惊讶",
}
EMOTE_ALLOWED = ["wave", "nod", "shake_head", "bow", "cheer", "think", "sulk"]


def _validate_action(action):
    """校验动作字段。返回 None 表示合法，否则返回错误文案。
    动作格式：{"kind": "gesture", "value": 0~7}
            {"kind": "expression", "value": 0.0~1.0, "name": 表情键}
            {"kind": "emote", "value": 名}
    """
    if action is None:
        return None
    if not isinstance(action, dict):
        return "动作格式不对（需要是对象）"
    kind = action.get("kind")
    if kind == "gesture":
        v = action.get("value")
        if not isinstance(v, int) or v not in GESTURE_LIBRARY:
            return f"手势值必须是 0~7 的整数（{GESTURE_LIBRARY}）"
        return None
    if kind == "expression":
        nm = action.get("name")
        if nm not in EXPRESSION_ALLOWED:
            return f"表情必须是：{'/'.join(EXPRESSION_ALLOWED)}"
        v = action.get("value", 1.0)
        try:
            v = float(v)
        except Exception:
            return "表情强度需要是数字"
        if not (0.0 <= v <= 1.0):
            return "表情强度要在 0~1 之间"
        action["value"] = v
        return None
    if kind == "emote":
        v = action.get("value")
        if v not in EMOTE_ALLOWED:
            return f"emote 必须是：{'/'.join(EMOTE_ALLOWED)}"
        return None
    return f"不认识的动作类型：{kind}（只支持 gesture/expression/emote）"


def _describe_action(action):
    """动作的人类可读描述。"""
    if not isinstance(action, dict):
        return "（无动作）"
    kind = action.get("kind")
    if kind == "gesture":
        return f"比手势「{GESTURE_LIBRARY.get(action.get('value'), '?')}」"
    if kind == "expression":
        return f"做表情「{EXPRESSION_ALLOWED.get(action.get('name'), '?')}」"
    if kind == "emote":
        return f"表演「{action.get('value')}」"
    return "（未知动作）"

# 名称/触发词的合法字符白名单（中文/字母/数字/空格/常用标点），拒绝危险字符
_ALLOWED = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9 _\-、，。！？!?：:；;（）()%～~]+$")
# 回应文本：允许更宽松（含 emoji/表情/语气符号），但拒绝换行与危险控制字符（防脚本注入）
_ALLOWED_REPLY = re.compile(r"^[^\x00-\x08\x0a-\x1f<>{}\\]+$")

_lib_path = os.path.join(CONFIG["data_dir"], "custom_skills.json")
_lock = threading.Lock()
_audit_path = os.path.join(CONFIG["data_dir"], "custom_skills_audit.jsonl")


def _load():
    try:
        with open(_lib_path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items):
    os.makedirs(os.path.dirname(_lib_path), exist_ok=True)
    with open(_lib_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _audit(action, name, detail=""):
    try:
        with open(_audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "action": action, "name": name, "detail": str(detail)[:200],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _validate(text, field, maxlen, reply=False):
    text = (text or "").strip()
    if not text:
        return f"{field} 不能为空"
    if len(text) > maxlen:
        return f"{field} 太长（最多 {maxlen} 字）"
    pat = _ALLOWED_REPLY if reply else _ALLOWED
    if not pat.match(text):
        return f"{field} 含有不允许的字符"
    return None


def list_skills():
    """返回所有自定义行为（只读副本）。"""
    return list(_load())


def _validate_trigger_meta(meta):
    """校验结构化触发条件。合法格式：
    {"type": "emotion", "dim": "joy", "th": 0.5}      # 她的情绪维度超阈值
    {"type": "keyword", "text": "加油"}                # 用户最近说过的话包含关键词
    {"type": "time",    "hours": [18,19,20,21,22]}    # 当前小时命中
    """
    if meta is None:
        return None
    if not isinstance(meta, dict):
        return "触发条件格式不对"
    t = meta.get("type")
    if t == "emotion":
        if meta.get("dim") not in EMOTION_DIMS_ALLOWED:
            return f"情绪维度必须是：{'/'.join(EMOTION_DIMS_ALLOWED)}"
        try:
            th = float(meta.get("th", 0.5))
        except Exception:
            return "情绪阈值需要是数字"
        meta["th"] = max(0.0, min(1.0, th))
        return None
    if t == "keyword":
        txt = (meta.get("text") or "").strip()
        if not txt or len(txt) > _MAX_TRIGGER:
            return "关键词不能为空且不能太长"
        if not _ALLOWED.match(txt):
            return "关键词含有不允许的字符"
        meta["text"] = txt
        return None
    if t == "time":
        hours = meta.get("hours")
        if not isinstance(hours, list) or not all(isinstance(h, int) and 0 <= h <= 23 for h in hours):
            return "时间段 hours 必须是 0~23 的整数列表"
        meta["hours"] = sorted(set(hours))[:24]
        return None
    return f"不认识的触发类型：{t}（只支持 emotion/keyword/time）"


# 情绪维度（触发条件用）
EMOTION_DIMS_ALLOWED = ["joy", "anger", "sadness", "calm", "anxiety"]


def add_skill(name, trigger, reply, action=None, trigger_meta=None):
    """新增一个自定义行为（可选携带动作与结构化触发条件）。返回 (ok, msg)。

    action: VRChat 动作 dict（见 _validate_action），None 表示纯文本行为。
    trigger_meta: 结构化触发条件（见 _validate_trigger_meta），None 表示按触发词匹配。
    """
    for field, val, mx in (("名称", name, _MAX_NAME),
                           ("触发词", trigger, _MAX_TRIGGER),
                           ("回应", reply, _MAX_REPLY)):
        err = _validate(val, field, mx, reply=(field == "回应"))
        if err:
            return False, err
    act_err = _validate_action(action)
    if act_err:
        return False, act_err
    meta_err = _validate_trigger_meta(trigger_meta)
    if meta_err:
        return False, meta_err
    with _lock:
        items = _load()
        for it in items:
            if it.get("name", "").strip() == name.strip():
                return False, f"已经有一个叫「{name.strip()}」的自定义行为了，换个名字或先删掉它吧～"
        entry = {
            "name": name.strip(),
            "trigger": trigger.strip(),
            "reply": reply.strip(),
            "created": datetime.now().isoformat(timespec="seconds"),
        }
        if action is not None:
            entry["action"] = action
        if trigger_meta is not None:
            entry["trigger_meta"] = trigger_meta
        items.append(entry)
        # 容量上限：超出则丢弃最老的（不影响其它）
        if len(items) > _MAX_SKILLS:
            items = items[-_MAX_SKILLS:]
        _save(items)
    _audit("add", name.strip(), detail=repr({"action": action, "meta": trigger_meta})[:300])
    if action is not None:
        return True, (f"我学会啦～以后{trigger.strip()}，我就{_describe_action(action)}。"
                      f"（回应：{reply.strip()}）")
    return True, f"我学会啦～以后你说「{trigger.strip()}」，我就会回应「{reply.strip()}」。"


def remove_skill(name):
    """删除一个自定义行为。返回 (ok, msg)。"""
    name = (name or "").strip()
    if not name:
        return False, "要删哪个自定义行为呀？告诉我名字～"
    with _lock:
        items = _load()
        kept = [it for it in items if it.get("name", "").strip() != name]
        if len(kept) == len(items):
            return False, f"我没有叫「{name}」的自定义行为，看看我有哪些吧～"
        _save(kept)
    _audit("remove", name)
    return True, f"好～我已经忘掉「{name}」这个自定义行为啦。"


def match_skill(text):
    """用用户这句话匹配一个自定义行为；命中返回 reply，否则 None。"""
    reply, _ = match_skill_with_action(text)
    return reply


def match_skill_with_action(text):
    """匹配自定义行为；命中返回 (reply, action)，否则 (None, None)。"""
    if not text:
        return None, None
    for it in _load():
        trig = (it.get("trigger") or "").strip()
        if trig and trig in text:
            return it.get("reply"), it.get("action")
    return None, None


def taught_rules():
    """返回所有「带动作」的教学规则（触发执行引擎用）。"""
    return [it for it in _load() if it.get("action") is not None]


def format_list():
    items = _load()
    if not items:
        return "我目前还没有自定义行为哦。你可以让我学会一个：比如「记下：你说『开工』，我就回复『好嘞开工！』」"
    lines = ["【我学会的自定义行为】"]
    for it in items:
        act = f"，并{_describe_action(it['action'])}" if it.get("action") else ""
        lines.append(f"- 触发「{it['trigger']}」 → 回应「{it['reply']}」{act}")
    return "\n".join(lines)
