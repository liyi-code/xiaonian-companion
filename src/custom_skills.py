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


def add_skill(name, trigger, reply):
    """新增一个自定义行为。返回 (ok, msg)。"""
    for field, val, mx in (("名称", name, _MAX_NAME),
                           ("触发词", trigger, _MAX_TRIGGER),
                           ("回应", reply, _MAX_REPLY)):
        err = _validate(val, field, mx, reply=(field == "回应"))
        if err:
            return False, err
    with _lock:
        items = _load()
        for it in items:
            if it.get("name", "").strip() == name.strip():
                return False, f"已经有一个叫「{name.strip()}」的自定义行为了，换个名字或先删掉它吧～"
        items.append({
            "name": name.strip(),
            "trigger": trigger.strip(),
            "reply": reply.strip(),
            "created": datetime.now().isoformat(timespec="seconds"),
        })
        # 容量上限：超出则丢弃最老的（不影响其它）
        if len(items) > _MAX_SKILLS:
            items = items[-_MAX_SKILLS:]
        _save(items)
    _audit("add", name.strip())
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
    if not text:
        return None
    for it in _load():
        trig = (it.get("trigger") or "").strip()
        if trig and trig in text:
            return it.get("reply")
    return None


def format_list():
    items = _load()
    if not items:
        return "我目前还没有自定义行为哦。你可以让我学会一个：比如「记下：你说『开工』，我就回复『好嘞开工！』」"
    lines = ["【我学会的自定义行为】"]
    for it in items:
        lines.append(f"- 触发「{it['trigger']}」 → 回应「{it['reply']}」")
    return "\n".join(lines)
