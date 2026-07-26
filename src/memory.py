import json
import os
import re
import math
import threading
from collections import Counter
from datetime import datetime
from config import CONFIG


# —— 零依赖中文/英文分词（本地版 venv 未装 jieba，用字符级兜底）——
# 中文：按字 unigram + 相邻 bigram（覆盖“小念/熬夜/原神”这类子串重叠）；
# 英文/数字：连续字母数字作为一个词。足够做关键词重叠检索，无需任何第三方库。
_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[a-z0-9]+")

# 中文停用字（高频虚词/语气词）：作为「单字」时信息量极低，容易制造噪声匹配，
# 检索时直接丢弃其单字形式，但保留由它们构成的 bigram（如“你好”仍有区分度）。
_CJK_STOP = set(
    "我的你他她它是不了在也都就还吧呀哦啊嘛呢吗呃嗯哈哎呦哇咦哟呗咧咯哒呐嘻好"
    "这那哪啥怎么什么有没有要会能可可以呗噢喔唷喂嘞撒拉咯哈凑呀哇嘢嘅"
    "的了着过给被把让叫跟和跟与或且若如果因为所以但不过然然后先再才刚"
    "很太最更比较有点些个们种次回遍下上中来去出进到从对没错啦啰噜喂"
)


def _tokenize(text):
    if not text:
        return []
    text = str(text).lower()
    toks = []
    # 拉丁词
    toks.extend(_LATIN.findall(text))
    # CJK 字符 unigram + bigram
    cjk = [c for c in text if _CJK.match(c)]
    if cjk:
        for c in cjk:
            if c not in _CJK_STOP:
                toks.append(c)  # unigram（跳过停用字单字）
        for i in range(len(cjk) - 1):
            toks.append(cjk[i] + cjk[i + 1])  # bigram（保留，更具区分度）
    return toks


class Memory:
    """长期记忆：记住关于用户的信息、小事，以及最近的对话。

    扩展（检索增强 + 长记忆压缩）：
    - archive：追加式完整对话转录（用户+助手都存），可远超 recent_history 的窗口，
      用于跨长时间跨度的语义检索（RAG grounding）。
    - _index：倒排索引 token -> [(条目序号, 词频)]，检索时 O(命中词) 而非全表扫描。
    - summaries：LLM 把旧对话压缩成的「长期记忆」要点，密集、优先召回。
    """

    def __init__(self, path=None):
        self.path = path or os.path.join(CONFIG["data_dir"], "memory.json")
        self.data = {"profile": {}, "facts": [], "history": [], "summaries": []}
        # 归档文件（追加式，独立文件避免 memory.json 被撑大）
        self.archive_path = os.path.join(CONFIG["data_dir"], "memory_archive.jsonl")
        self._archive = []          # [{"i","role","content","time"}]
        self._index = {}            # token -> [[i, tf], ...]
        self._df = {}               # token -> 文档频数
        self._next_id = 0
        self._wlock = threading.Lock()  # 保护 memory.json 与归档文件的并发写入
        self.load()
        self._rebuild_index()

    # ——————————————————————— 基础持久化 ———————————————————————
    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                pass
        self.data.setdefault("profile", {})
        self.data.setdefault("facts", [])
        self.data.setdefault("history", [])
        self.data.setdefault("summaries", [])
        # 加载归档
        if os.path.exists(self.archive_path):
            try:
                with open(self.archive_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self._archive.append(json.loads(line))
                        except Exception:
                            continue
            except Exception:
                pass
        self._next_id = (self._archive[-1]["i"] + 1) if self._archive else 0

    def save(self):
        with self._wlock:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

    def _append_archive(self, entry):
        with self._wlock:
            try:
                with open(self.archive_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def _rebuild_index(self):
        self._index = {}
        self._df = {}
        for e in self._archive:
            toks = _tokenize(e.get("content", ""))
            tf = Counter(toks)
            seen = set()
            for t, c in tf.items():
                self._index.setdefault(t, []).append([e["i"], c])
                if t not in seen:
                    self._df[t] = self._df.get(t, 0) + 1
                    seen.add(t)

    # ——————————————————————— 写入 ———————————————————————
    def remember_fact(self, fact):
        fact = fact.strip()
        if fact and fact not in self.data["facts"]:
            self.data["facts"].append(fact)
            self.save()

    def set_profile(self, key, value):
        self.data["profile"][key] = value
        self.save()

    def add_message(self, role, content, to_history=True):
        """写一条对话。

        to_history=True：同时进入 recent_history（用于直接上下文，旧行为）。
        to_history=False：只进归档（用于用户消息——避免 recent_history 里
            出现重复的用户轮，同时让 RAG 能检索到用户说过的话）。
        两种都会写入追加式 archive 供检索。
        """
        if to_history:
            self.data["history"].append({
                "role": role,
                "content": content,
                "time": datetime.now().isoformat(timespec="seconds"),
            })
            if len(self.data["history"]) > 200:
                self.data["history"] = self.data["history"][-200:]
            self.save()
        self._archive_message(role, content)

    def _archive_message(self, role, content):
        entry = {
            "i": self._next_id,
            "role": role,
            "content": content,
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        self._next_id += 1
        self._archive.append(entry)
        # 增量更新索引（不必全量重建）
        toks = _tokenize(content)
        tf = Counter(toks)
        seen = set()
        for t, c in tf.items():
            self._index.setdefault(t, []).append([entry["i"], c])
            if t not in seen:
                self._df[t] = self._df.get(t, 0) + 1
                seen.add(t)
        self._append_archive(entry)
        # 归档上限保护：超出则丢弃最老的若干条（其信息通常已被压缩进摘要）
        cap = int(CONFIG.get("memory_archive_cap", 20000))
        if len(self._archive) > cap:
            drop = len(self._archive) - cap
            self._archive = self._archive[drop:]
            self._rebuild_index()
            # 归档被截断后，压缩进度指针若停在已丢弃的 id 区间之外，会导致后续压缩
            # 段为空、静默失效。把它夹回“最老保留条目的 id”，让压缩从保留段重新进行。
            if self._archive:
                min_ret = self._archive[0]["i"]
                if self.data.get("_last_compress_idx", 0) > min_ret:
                    self.data["_last_compress_idx"] = min_ret
            # 重写归档文件（持锁，避免与并发追加互相穿插）
            with self._wlock:
                try:
                    with open(self.archive_path, "w", encoding="utf-8") as f:
                        for e in self._archive:
                            f.write(json.dumps(e, ensure_ascii=False) + "\n")
                except Exception:
                    pass

    # ——————————————————————— 近期历史（原接口不变） ———————————————————————
    def recent_history(self, n=20):
        return self.data["history"][-n:]

    def profile_text(self):
        lines = []
        if self.data["profile"]:
            lines.append("【关于你的信息】")
            for k, v in self.data["profile"].items():
                lines.append(f"- {k}: {v}")
        if self.data["facts"]:
            lines.append("【我记住的小事】")
            for f_ in self.data["facts"][-20:]:
                lines.append(f"- {f_}")
        return "\n".join(lines) if lines else "（我还不太了解你，慢慢告诉我吧～）"

    # ——————————————————————— 长期记忆摘要（压缩产物） ———————————————————————
    def add_summary(self, text, advance_to=None):
        text = (text or "").strip()
        if text:
            self.data["summaries"].append({
                "text": text,
                "time": datetime.now().isoformat(timespec="seconds"),
            })
            # 摘要也进归档索引，便于后续检索命中
            self._archive_message("summary", text)
            # 压缩进度指针：由调用方（assistant._compress_worker）按「已压缩段末尾 id」推进，
            # 不要在这里跳到整个归档末尾，否则会把未压缩的旧片段一并标记为“已压缩”，
            # 导致长期记忆压缩只跑一次就停（历史 bug）。
            if advance_to is not None:
                self.data["_last_compress_idx"] = int(advance_to)
            self.save()

    @property
    def last_compress_idx(self):
        return int(self.data.get("_last_compress_idx", 0))

    def archive_len(self):
        return len(self._archive)

    def next_id(self):
        """当前下一个待分配归档 id（= 已分配最大 id + 1）。

        压缩进度、段边界都应按「id 空间」计算，而非 archive_len()（计数）。
        正常情况下两者相等；但归档达到 cap 被截断后，archive_len() 会小于 next_id，
        此时若用 archive_len() 当边界会把压缩指针算错（见 assistant._maybe_compress）。
        """
        return self._next_id

    def get_archive(self):
        """返回归档条目的浅拷贝列表（供 GUI 查看面板只读展示用）。"""
        return list(self._archive)

    def archive_slice(self, start_i, end_i):
        """返回 i 在 [start_i, end_i) 之间的归档条目（按时间顺序）。"""
        return [e for e in self._archive if start_i <= e["i"] < end_i]

    # ——————————————————————— 检索（RAG grounding） ———————————————————————
    def retrieve(self, query, k=4, exclude_last=16):
        """从归档里检索与 query 最相关的片段，作为 grounding 注入 prompt。

        - 排除最近 exclude_last 条（它们已在 recent_history 里，无需重复）；
        - 用 TF-IDF 打分（中文 bigram 重叠天然捕捉子串相关）；
        - 同时召回最相关的「长期记忆摘要」（密集知识，权重加成），
          让“很久以前说过的事”也能被想起。
        返回格式化文本；无结果返回空串。
        """
        qtoks = _tokenize(query)
        if not qtoks:
            return ""
        N = len(self._archive)
        if N == 0:
            return ""
        # query 端用「唯一 token 集合」打分（binary query freq）：避免“天”在 query
        # 里重复出现就把分值翻倍越过阈值，造成无关片段误召回。
        qt_uniq = set(qtoks)
        # 候选打分（归档条目）
        min_score = float(CONFIG.get("rag_min_score", 2.0))
        scores = {}
        for t in qt_uniq:
            postings = self._index.get(t)
            if not postings:
                continue
            idf = math.log((N + 1) / (self._df.get(t, 0) + 1)) + 1.0
            # 权重：bigram（len>=2，更具体、区分度高）给满权；单字/CJK unigram 降权，
            # 抑制“天/好”这类高频单字在小型语料里 idf 虚高造成的噪声匹配。
            w = 1.0 if len(t) >= 2 else 0.5
            for i, tf in postings:
                if i >= N - exclude_last:
                    continue  # 最近窗口已直接进 prompt
                s = tf * idf * w
                if s < min_score:
                    continue  # 相关性过低，视为噪声不注入
                scores[i] = scores.get(i, 0.0) + s
        # 候选打分（摘要，权重加成，且不受最近窗口限制）
        sum_scores = {}
        for s in self.data["summaries"]:
            stoks = _tokenize(s["text"])
            if not stoks:
                continue
            stf = Counter(stoks)
            sc = 0.0
            overlap = 0
            for t in qt_uniq:
                if t in stf:
                    overlap += 1
                    idf = math.log((N + 1) / (self._df.get(t, 0) + 1)) + 1.0
                    w = 1.0 if len(t) >= 2 else 0.5
                    sc += stf[t] * idf * w
            if overlap > 0:
                sum_scores[s["text"]] = sc * 1.5  # 摘要密集，加成优先

        # 输出：摘要优先（最多 2 条），再补对话片段（凑满 k）
        out = []
        if sum_scores:
            for txt, _ in sorted(sum_scores.items(), key=lambda x: x[1], reverse=True)[:2]:
                out.append(f"【长期记忆】{txt}")
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        for i, _ in ranked:
            if len(out) >= k:
                break
            e = self._archive[i]
            who = "你" if e["role"] == "user" else ("小念" if e["role"] == "assistant" else "记忆")
            snippet = e["content"].replace("\n", " ")
            if len(snippet) > 120:
                snippet = snippet[:120] + "…"
            out.append(f"（{who}）{snippet}")
        return "\n".join(out)
