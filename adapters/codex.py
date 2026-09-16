"""Codex Desktop / Codex CLI 适配器。

出站：扫 ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
入站：暂未打通（Codex 没有 Claude 那种 UDS 通道，留待后续）
"""

import glob
import json
import os
import time

from .base import Adapter, text_from_content

ROOT = os.path.expanduser("~/.codex/sessions")
GLOB = os.path.join(ROOT, "*", "*", "*", "rollout-*.jsonl")

_summary_cache = {}


def _iter_jsonl(path, max_lines=None):
    try:
        with open(path, errors="replace") as fh:
            for i, line in enumerate(fh):
                if max_lines is not None and i >= max_lines:
                    return
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _payload(entry):
    p = entry.get("payload")
    return p if isinstance(p, dict) else {}


def _is_real_user(p):
    """Codex 会把上下文（AGENTS.md、插件清单等）也塞成 role=user。
    真实用户输入带 content_item_kinds=["user.text"]，据此区分。"""
    meta = p.get("internal_chat_message_metadata_passthrough") or {}
    kinds = meta.get("content_item_kinds")
    if not kinds:
        return True  # 老会话没有这个字段，不过滤
    return "user.text" in kinds


class CodexAdapter(Adapter):
    name = "codex"
    label = "Codex"

    def __init__(self):
        if not os.path.isdir(ROOT):
            raise RuntimeError("找不到 %s" % ROOT)

    # ---------- 内部 ----------

    def _path_for(self, sid):
        hits = []
        for path in glob.glob(GLOB):
            stem = os.path.basename(path)
            if sid in stem:
                hits.append(path)
                continue
            for entry in _iter_jsonl(path, max_lines=1):
                if _payload(entry).get("session_id") == sid:
                    hits.append(path)
                break
        if not hits:
            return None
        return max(hits, key=os.path.getmtime)

    def _summarize(self, path):
        mtime = os.path.getmtime(path)
        cached = _summary_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1], cached[2], cached[3]

        sid, cwd, title = None, None, None
        for entry in _iter_jsonl(path, max_lines=500):
            if entry.get("type") == "session_meta":
                p = _payload(entry)
                sid = p.get("session_id") or p.get("id")
                cwd = p.get("cwd")
            elif entry.get("type") == "response_item" and title is None:
                p = _payload(entry)
                if p.get("type") == "message" and p.get("role") == "user" \
                        and _is_real_user(p):
                    text = text_from_content(p.get("content"))
                    if text.strip():
                        title = text.strip().replace("\n", " ")[:80]
        if sid is None:
            sid = os.path.basename(path).split("-")[-1].replace(".jsonl", "")
        _summary_cache[path] = (mtime, sid, cwd, title)
        return sid, cwd, title

    # ---------- 接口 ----------

    def sessions(self, limit=80):
        paths = sorted(glob.glob(GLOB), key=os.path.getmtime, reverse=True)[:limit]
        out = []
        for path in paths:
            try:
                sid, cwd, title = self._summarize(path)
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            out.append({
                "app": self.name,
                "id": sid,
                "title": title or "(无标题)",
                "cwd": cwd or "",
                "mtime": mtime,
                "live": False,
                "can_send": False,
            })
        return out

    def messages(self, sid, limit=400):
        path = self._path_for(sid)
        if not path:
            return []
        out = []
        for entry in _iter_jsonl(path):
            if entry.get("type") != "response_item":
                continue
            p = _payload(entry)
            if p.get("type") != "message":
                continue
            role = p.get("role")
            if role not in ("user", "assistant"):
                continue
            if role == "user" and not _is_real_user(p):
                continue
            text = text_from_content(p.get("content"))
            if not text.strip():
                continue
            out.append({
                "role": role,
                "text": text,
                "ts": entry.get("timestamp"),
                "via": None,
            })
        start = max(0, len(out) - limit)
        tail = out[start:]
        # 绝对序号：界面靠它对账新消息。返回的是滑动窗口（最后 limit 条），
        # 光看长度判断"有没有新的"在窗口满了之后会永远判成没有。
        for n, item in enumerate(tail):
            item["i"] = start + n
        return tail

    def can_send(self, sid):
        return False

    def blocked_reason(self, sid):
        return "Codex 的入站通道还没打通，目前只能读"

    def send(self, sid, text):
        raise RuntimeError("Codex 的入站通道还没打通，目前只能读")
