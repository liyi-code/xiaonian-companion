"""把各平台 transport 收到的消息喂给小念大脑，并把回复原路发回。

  - 桌面窗口（GUI）继续用 Assistant 的 owner_session；
  - QQ / 微信 收到消息时，按 (平台, 用户) 建独立 Session，互不串台；
  - 主人的 QQ/微信（配置里的 QQ_OWNER / WECHAT_OWNER）复用 owner_session，
    这样在桌面和 IM 里聊的是同一段记忆。
"""

import re
import os
import logging
import threading

from config import CONFIG
from assistant import Assistant, Session
from memory import Memory
from transport.registry import register, all_transports


def _session_path(key):
    safe = re.sub(r"[^0-9A-Za-z_]", "_", key)
    return os.path.join(CONFIG["data_dir"], f"memory_{safe}.json")


class Bot:
    def __init__(self, assistant: Assistant):
        self.assistant = assistant
        self.sessions = {}          # (平台,用户) -> Session
        self._lock = threading.Lock()
        self._log = logging.getLogger("bot")

    def setup(self):
        # ---- QQ ----
        if CONFIG.get("qq_enabled"):
            try:
                from transport.qq_onebot import QQOneBot
                t = QQOneBot(
                    CONFIG["qq_ws_url"],
                    on_message=self._on_message,
                    token=CONFIG.get("qq_token", ""),
                )
                register(t)
                self._log.info("QQ 接入已注册：%s", CONFIG["qq_ws_url"])
            except Exception as e:
                self._log.warning("QQ 接入初始化失败：%s", e)
        # ---- 微信（后续版本接入，这里预留占位）----
        if CONFIG.get("wechat_enabled"):
            self._log.info("微信接入将在后续版本启用（gewechat / padlocal）")
        return self

    def start(self):
        for t in all_transports():
            try:
                t.start()
                self._log.info("%s 已启动", t.platform)
            except Exception as e:
                self._log.warning("%s 启动失败：%s", t.platform, e)

    # --------------------------- 会话管理 --------------------------- #
    def _session_for(self, platform, user_id):
        owner_id = CONFIG.get(f"{platform.lower()}_owner", "")
        if owner_id and str(user_id) == str(owner_id):
            return self.assistant.owner_session
        key = f"{platform}:{user_id}"
        with self._lock:
            s = self.sessions.get(key)
            if s is None:
                mem = Memory(path=_session_path(key))
                s = Session(mem, is_owner=False)
                self.sessions[key] = s
            return s

    # --------------------------- 消息回调 --------------------------- #
    def _on_message(self, platform, user_id, text, name, group_id=None, raw=None):
        self._log.info("[%s] %s(%s): %s", platform, name, user_id, text[:50])
        session = self._session_for(platform, user_id)
        try:
            reply = self.assistant.chat(text, session=session)
        except Exception as e:
            self._log.warning("大脑处理出错：%s", e)
            reply = "（小念走神了一下，稍后再试～）"

        # 原路回复
        t = next((tr for tr in all_transports() if tr.platform == platform), None)
        if t is None:
            return
        try:
            if group_id:
                t.send_to_user(user_id, reply, group_id=group_id)
            else:
                t.send_to_user(user_id, reply)
        except Exception as e:
            self._log.warning("回复发送失败：%s", e)
