# 让小念上 QQ（OneBot v11 + go-cqhttp）

小念有自己的 QQ 号后，用 go-cqhttp 把它接进来，她就能在 QQ 里陪你聊天、被你呼来喝去。

## 1. 下载 go-cqhttp
到 https://github.com/Mrs4s/go-cqhttp/releases 下载对应系统版本，解压。

## 2. 配置（正向 WebSocket）
把本目录的 `config.yml.example` 复制为 `config.yml`，修改：
- `account.uin`：改成**小念的 QQ 号**
- `servers` 里已有 `ws: address: 127.0.0.1:6700`（本项目默认连这个端口）

如需鉴权，给 `default-middlewares.access-token` 设值，并在本项目 `.env` 的 `QQ_TOKEN` 填一样的值。

## 3. 登录
运行 `go-cqhttp`（首次生成 `device.json` 等），用手机 QQ 扫码登录小念的号。
看到日志出现 WebSocket 监听在 `127.0.0.1:6700` 即成功。

## 4. 配置本项目 `.env`
```
QQ_ENABLED=true
QQ_WS_URL=ws://127.0.0.1:6700
QQ_TOKEN=            # 若第2步设了 token 就填一样的
QQ_OWNER=你的QQ号     # 主人的 QQ，复用桌面窗口的那段记忆
```

## 5. 运行
正常启动 `python src/main.py`。小念窗口照常打开，同时她也会在 QQ 里回你。
用你的 QQ 给小念的号发条消息试试即可。

## 常见问题
- 连不上：确认 go-cqhttp 已运行、端口是 6700、token 一致。
- 收不到消息：go-cqhttp 可能触发风控要求设备锁/短信验证，按提示在手机端处理。
- 多人在 QQ 里加小念：每个 QQ 用户会有独立的会话记忆（主人号除外，共享桌面记忆）。
