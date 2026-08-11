# -*- coding: utf-8 -*-
"""小念的网络搜索模块。

让小念能自主搜索互联网获取信息，以满足用户需求：
- 遇到不会做的事→搜索该怎么实现
- 需要实时信息→搜索获取最新数据
- 需要知识→搜索学习

支持搜索引擎：DuckDuckGo Lite（默认，不需 API key）、可配备用引擎。
代理：自动从环境变量 UPDATE_PROXY / HTTP_PROXY / 系统代理发现。
"""

import urllib.request
import urllib.parse
import urllib.error
import re
import os
import json
from html import unescape
from config import CONFIG

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_SEARCH_URL = CONFIG.get("search_endpoint") or "https://lite.duckduckgo.com/lite/"
_TIMEOUT = 12


# ---- 代理自动发现 ----

def _find_proxy():
    """自动发现可用的 HTTP/HTTPS 代理。优先级：UPDATE_PROXY > 环境变量 > Windows 系统代理。"""
    # 1) 显式代理
    for key in ("UPDATE_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    # 2) Windows 系统代理（Clash / V2Ray 之类通常写这里）
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            if enabled:
                server = winreg.QueryValueEx(key, "ProxyServer")[0]
                if server and not server.startswith("http"):
                    server = "http://" + server
                return server
    except Exception:
        pass
    return None


def _build_opener(url):
    """创建带代理的 opener。"""
    proxy = _find_proxy()
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


# ---- HTML 工具 ----

def _strip_html(html):
    """去除 HTML 标签，保留文字。"""
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---- DuckDuckGo Lite 解析 ----

def _parse_ddg_lite(html, max_results=5):
    """解析 DuckDuckGo Lite 返回的搜索结果。"""
    results = []
    # Lite 版格式：<a href="...">标题</a> ... <span class="link-text">URL</span> ... <td>摘要</td>
    # 更鲁棒：用正则提取每组 (href, title, snippet)
    blocks = re.findall(
        r'<a\s[^>]*href="([^"]+)"[^>]*>([^<]+)</a>.*?'
        r'(?:<span[^>]*class="[^"]*link-text[^"]*"[^>]*>([^<]*)</span>)?'
        r'(?:.*?<td[^>]*class="[^"]*(?:result-snippet|snippet)[^"]*"[^>]*>(.*?)</td>)?',
        html, re.DOTALL,
    )
    for href, title, _, snippet in blocks[:max_results]:
        url = urllib.parse.unquote(href) if href.startswith("//") or "uddg=" in href else href
        if "uddg=" in url:
            m = re.search(r"[?&]uddg=([^&]+)", url)
            if m:
                url = urllib.parse.unquote(m.group(1))
        title = _strip_html(title).strip()
        snippet = _strip_html(snippet or "").strip()
        if title and url.startswith("http"):
            results.append({"title": title[:120], "url": url, "snippet": snippet[:200]})
    return results


# ---- 搜索入口 ----

def search(query, max_results=5):
    """搜索互联网，返回结果列表 [{title, url, snippet}, ...]。"""
    query = (query or "").strip()
    if not query:
        return []
    params = urllib.parse.urlencode({"q": query.encode("utf-8")})
    req = urllib.request.Request(
        f"{_SEARCH_URL}?{params}",
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
    )
    opener = _build_opener(_SEARCH_URL)
    try:
        with opener.open(req, timeout=_TIMEOUT) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        # 搜索失败返回空，不抛异常（小念后续会自行降级）
        return [{"error": str(e)[:100]}]
    return _parse_ddg_lite(html, max_results)


def fetch(url, timeout=_TIMEOUT):
    """获取指定网页内容（纯文本摘要）。"""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return f"无效 URL：{url}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    opener = _build_opener(url)
    try:
        with opener.open(req, timeout=timeout) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "text" not in ct and "html" not in ct:
                return f"（该页面不是文本/网页，Content-Type: {ct}）"
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"获取页面失败：{e}"
    # 提取正文（去除 script/style）
    for tag in ("script", "style", "nav", "footer", "header"):
        html = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", " ", html, flags=re.DOTALL | re.I)
    text = _strip_html(html)
    # 截断过长
    if len(text) > 3000:
        text = text[:3000] + "…（内容过长已截断）"
    return text


def format_results(results, query=""):
    """把搜索结果格式化成小念可读的文本。"""
    if not results:
        return f"没有搜到关于「{query}」的结果～"
    if "error" in results[0]:
        return f"搜索遇到问题（{results[0]['error']}），可能是网络不通或搜索引擎暂时不可用。"
    lines = [f"【搜索「{query}」的结果】"]
    for i, r in enumerate(results, 1):
        snip = f" — {r['snippet']}" if r.get("snippet") else ""
        lines.append(f"{i}. {r['title']}{snip}\n   {r['url']}")
    return "\n".join(lines)
