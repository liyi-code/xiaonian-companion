"""QQ 接入：OneBot v11 正向 WebSocket 客户端（对接 go-cqhttp）。

部署步骤（详见 onebot/README.md）：
  1) 下载 go-cqhttp，配置文件用“正向 WS”监听 127.0.0.1:6700；
  2) 运行 go-cqhttp，扫码登录【小念的 QQ】；
  3) 本项目 .env 里设 QQ_ENABLED=true 与 QQ_WS_URL=ws://127.0.0.1:6700。

小念在 QQ 里收到私聊 / 群聊 -> 交给大脑 -> 原路回复。
"""

import json
import re
import time
import threading
import logging

try:
    from websocket import WebSocketApp
except Exception:  # pragma: no cover
    WebSocketApp = None

from transport.base import BotTransport

_CQ = re.compile(r"\[CQ:[^\]]*\]")


def _strip_cq(text):
    """去掉 go-cqhttp 的 CQ 码（图片/表情等），只留纯文本。"""
    return _CQ.sub("", text or "").strip()


def _as_qq(s):
    s = (s or "").strip()
    return int(s) if s.isdigit() else None


class QQOneBot(BotTransport):
    platform = "QQ"

    def __init__(self, url, on_message, token=""):
        super().__init__(on_message)
        self.url = url
        self.token = token
        self._ws = None
        self._echo = 0
        self._pending = {}          # echo -> {"event", "result"}
        self._lock = threading.Lock()
        self._thread = None

    # ----------------------------- 生命周期 ----------------------------- #
    def start(self):
        if WebSocketApp is None:
            self._log.error("未安装 websocket-client，QQ 接入不可用（pip install websocket-client）")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def _run(self):
        while self.running:
            try:
                self._log.info("连接 go-cqhttp: %s", self.url)
                self._ws = WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                if self.token:
                    self._ws.url = f"{self.url}?access_token={self.token}"
                self._ws.run_forever()
            except Exception as e:
                self._log.warning("WS 运行异常: %s", e)
            if not self.running:
                break
            time.sleep(3)

    # ------------------------------ 事件 ------------------------------- #
    def _on_open(self, ws):
        self._log.info("已连上 go-cqhttp，开始接收消息")

    def _on_error(self, ws, err):
        self._log.warning("WS 错误: %s", err)

    def _on_close(self, ws, *a):
        self._log.info("WS 连接关闭")

    def _on_message(self, ws, raw):
        try:
            data = json.loads(raw)
        except Exception:
            return
        # API 调用响应（带 echo）
        if "echo" in data and ("status" in data or "retcode" in data):
            self._resolve_echo(data)
            return
        # 心跳 / 生命周期等元事件忽略
        if data.get("post_type") == "meta_event":
            return
        if data.get("post_type") == "message":
            self._handle_message(data)

    def _handle_message(self, data):
        mtype = data.get("message_type")
        if mtype not in ("private", "group"):
            return
        text = _strip_cq(data.get("message", ""))
        if not text:
            return
        user_id = data.get("user_id")
        sender = data.get("sender", {}) or {}
        name = sender.get("nickname") or str(user_id)
        group_id = data.get("group_id") if mtype == "group" else None
        self.on_message(self.platform, user_id, text, name, group_id=group_id, raw=data)

    def _resolve_echo(self, data):
        eid = data.get("echo")
        with self._lock:
            fut = self._pending.pop(eid, None)
        if fut:
            fut["event"].set()
            fut["result"] = data

    # ----------------------------- API 调用 ---------------------------- #
    def _api(self, action, params, timeout=15):
        if not self._ws or not self.running:
            return None
        with self._lock:
            self._echo += 1
            eid = str(self._echo)
            fut = {"event": threading.Event(), "result": None}
            self._pending[eid] = fut
        payload = {"action": action, "params": params, "echo": eid}
        if self.token:
            payload["access_token"] = self.token
        try:
            self._ws.send(json.dumps(payload))
        except Exception as e:
            with self._lock:
                self._pending.pop(eid, None)
            self._log.warning("发送 API 失败: %s", e)
            return None
        fut["event"].wait(timeout)
        return fut["result"]

    # ------------------------------ 发送 ------------------------------- #
    def send_to_user(self, user_id, text, group_id=None):
        if group_id:
            r = self._api("send_group_msg", {"group_id": int(group_id), "message": text})
        else:
            r = self._api("send_private_msg", {"user_id": int(user_id), "message": text})
        if r is None:
            return (False, "未连接到 QQ（go-cqhttp 未运行或已断线）。")
        if r.get("status") == "ok" and r.get("retcode") == 0:
            return (True, "已发送")
        return (False, str(r.get("msg", r.get("wording", r))))

    def send_to_contact(self, contact, text):
        """按 QQ 号直接发；否则按昵称/备注在好友、群列表里最佳努力解析。"""
        cid = _as_qq(contact)
        if cid:
            return self.send_to_user(cid, text)
        friends = self._api("get_friend_list", {})
        if friends and friends.get("status") == "ok":
            for f in friends.get("data", []) or []:
                if f.get("nickname") == contact or f.get("remark") == contact:
                    return self.send_to_user(f["user_id"], text)
        groups = self._api("get_group_list", {})
        if groups and groups.get("status") == "ok":
            for g in groups.get("data", []) or []:
                if g.get("group_name") == contact:
                    return self.send_to_user(None, text, group_id=g["group_id"])
        return (False, f"没找到 QQ 联系人/群「{contact}」，请确认名字或改用 QQ 号。")
