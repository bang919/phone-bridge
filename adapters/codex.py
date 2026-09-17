"""Codex Desktop 适配器。

出站：扫 ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
入站：codex queue --thread <id> --message <text>

入站这条是**走 Codex 自己的 app-server**：App 里那个 `codex app-server` 进程
在 `~/.codex/ipc/ipc.sock` 上听，`codex queue` 是把消息交给它的官方入口。
那个 socket 认对端身份——用别的语言连上去（python 之类）发什么都没用，
发一个字节就被关；只有这个 App 自带的、签过名的 `codex` 二进制能进。
所以这里 shell 出去调它，不自己拼协议。
"""

import glob
import json
import os
import shutil
import subprocess
import threading
import time

from .base import Adapter, text_from_content

ROOT = os.path.expanduser("~/.codex/sessions")
GLOB = os.path.join(ROOT, "*", "*", "*", "rollout-*.jsonl")

# **macOS 刻意不用 PATH 里那个 codex。** PATH 上版本可能跟 App 实际跑的不一致，
# 而 ipc.sock 认的是对端身份，发命令的必须是这个 App 自己的二进制。Windows
# 的命名管道由官方 CLI 处理，优先使用 PATH / LocalAppData 里可运行的版本。
MAC_CODEX_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex"
IS_WINDOWS = os.name == "nt"
_codex_bin_cache = {"path": None}

# 列表每 5 秒轮询一次，所以要跟得上——但扫全部 rollout 的头一行要 0.3 秒，
# 每回都重扫就是白烧 CPU（实测 /api/sessions 会到 0.87 秒）。
#
# 办法是**按文件缓存头部**：`session_meta` 永远在文件第一行，往后追加内容不会
# 改它，所以按 mtime 缓存是安全的。重扫时只有新出现的文件要真读，其余全命中缓存
# ——这样 TTL 就能压到跟轮询同频。**不然电脑上刚建的对话，手机上要等 30 秒
# 才出现。**
INDEX_TTL = 5

_index_cache = {"at": 0.0, "threads": None}
_index_lock = threading.Lock()
_meta_cache = {}
_title_cache = {}

# Codex 会把上下文塞成 role=user。两道判据都要过——**各有各的漏**：
# `content_item_kinds` 有的消息根本没有（老版本写的不带），
# 而正文前缀跟版本无关，但认不出将来新增的种类。
_INJECTED_KINDS = ("agents_md.instructions", "environments.environment_context")
_INJECTED_PREFIXES = (
    "<recommended_plugins>",
    "<environment_context>",
    "<user_instructions>",
)

# 子代理也是独立 rollout，而且一次能带三十个（实测一个会话挂了 30 个
# guardian）。它们是内部机器，不是用户开的对话，从手机上给子代理发消息也没
# 意义——不进列表。
_HIDDEN_THREAD_SOURCES = ("subagent", "guardian_review")


def _codex_bin():
    if _codex_bin_cache["path"]:
        return _codex_bin_cache["path"]
    override = os.environ.get("PHONE_BRIDGE_CODEX")
    if override:
        _codex_bin_cache["path"] = override
        return override
    if not IS_WINDOWS:
        _codex_bin_cache["path"] = MAC_CODEX_BIN
        return MAC_CODEX_BIN

    local = os.environ.get("LOCALAPPDATA")
    if local:
        hits = glob.glob(os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe"))
        if hits:
            _codex_bin_cache["path"] = max(hits, key=os.path.getmtime)
            return _codex_bin_cache["path"]
    hit = shutil.which("codex")
    if hit:
        _codex_bin_cache["path"] = hit
        return hit

    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    hits = glob.glob(os.path.join(
        pf, "WindowsApps", "OpenAI.Codex_*", "app", "resources", "codex.exe"
    ))
    if hits:
        _codex_bin_cache["path"] = max(hits, key=os.path.getmtime)
        return _codex_bin_cache["path"]
    return None


def _iter_jsonl(path, max_lines=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
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


def _head_meta(path):
    """只读头一行拿 session_meta。扫全量时每个文件只读一行，快得多。"""
    for entry in _iter_jsonl(path, max_lines=1):
        if entry.get("type") == "session_meta":
            return _payload(entry)
    return {}


def _is_real_user(p):
    """这条 role=user 是用户打的，还是 Codex 塞的上下文。"""
    meta = p.get("internal_chat_message_metadata_passthrough") or {}
    kinds = meta.get("content_item_kinds") or []
    if any(k in _INJECTED_KINDS for k in kinds):
        return False
    text = text_from_content(p.get("content")).lstrip()
    if text.startswith(_INJECTED_PREFIXES):
        return False
    if not kinds:
        return True  # 老会话没有这个字段；正文前缀那道已经过了
    return "user.text" in kinds


def _clean_display(text):
    """剥掉图片的包装标签，只留正文。

    贴图的消息正文长这样，前后两行是 Codex 加的：

        <image name=[Image #1] path="/var/folders/…/codex-clipboard-M1OPyY.png">
        </image>
        [Image #1] 帮我看看这个怎么改？

    中间那个临时文件路径又长又没法点，画在气泡里纯是噪音；`[Image #1]`
    本身还在正文里，用户照样知道说的是哪张图。**按行剥**，不用正则去配
    标签块——配歪了会连正文一起吃掉。
    """
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("<image ") or s == "</image>":
            continue
        keep.append(line)
    return "\n".join(keep).strip()


def _messages_of(path):
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
        text = _clean_display(text_from_content(p.get("content")))
        if not text:
            continue
        out.append({
            "role": role,
            "text": text,
            "ts": entry.get("timestamp"),
            "via": None,
        })
    return out


def _merge_segments(files):
    """把同一个线程的分段文件拼成一段完整对话。

    分段不是"往同一个文件追加"，是**每段一个窗口**：resume / 重开窗口会新起
    一个 rollout，里面只有当时那一段。实测那个 13 段的线程，最新那份**只有 1
    条消息**——只看最新文件的话，用户最常接着聊的会话在手机上反而几乎空白
    （248 个桌面线程平均：只看最新 67.6 条，拼起来 86.7 条）。

    拼法是**往前补**：以最新那份为底，再拿更老的文件往前找，只补"还没出现过
    的开头若干条"，一撞上已有的就停。只增不减，所以不会漏；撞上就停，所以重复的
    那段不会被糊上去。**用户真的重复发同一句话也不会被吃掉**——逐条比内容，
    不是整段去重（整段去重会把"今天天气不错"发两次的那条吞掉一条）。
    """
    files = sorted(files, key=os.path.getmtime)
    acc = _messages_of(files[-1])
    seen = {(m["role"], m["text"]) for m in acc}
    for path in reversed(files[:-1]):
        older = _messages_of(path)
        i = 0
        while i < len(older) and (older[i]["role"], older[i]["text"]) not in seen:
            i += 1
        if not i:
            continue
        head = older[:i]
        for m in head:
            seen.add((m["role"], m["text"]))
        acc = head + acc
    return acc


def _head_meta_cached(path):
    """读文件头一行拿 session_meta，按 (path, mtime) 缓存。

    第一行不会变（追加只往后写），所以 mtime 没动就直接用缓存的。
    """
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, 0.0
    hit = _meta_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1], mtime
    meta = _head_meta(path)
    _meta_cache[path] = (mtime, meta)
    return meta, mtime


def _build_index():
    """扫一遍所有 rollout，按**线程 id** 归并。

    这里踩过一个坑：原来拿 `payload.session_id` 当行 id。但一个会话的子代理
    也各写自己的 rollout，它们的 `session_id` 指向**父**会话、`id` 才是自己
    ——于是十几个子代理全塌成父会话那一行，列表里同一个 id 反复出现、还互相
    顶掉（80 行只剩 37 个唯一 id）。行 id 必须是 `payload.id`。
    """
    threads = {}
    live_paths = set(glob.glob(GLOB))
    for path in live_paths:
        meta, mtime = _head_meta_cached(path)
        if meta is None:
            continue
        if meta.get("thread_source") in _HIDDEN_THREAD_SOURCES:
            continue
        tid = meta.get("id") or meta.get("session_id")
        if not tid:
            tid = os.path.basename(path).split("-")[-1].replace(".jsonl", "")
        t = threads.get(tid)
        if t is None:
            threads[tid] = {
                "id": tid,
                "files": [path],
                "mtime": mtime,
                "cwd": meta.get("cwd"),
                "originator": meta.get("originator"),
            }
            continue
        t["files"].append(path)
        if mtime >= t["mtime"]:
            # 会话的现场以最新那段为准（它才是当前状态）
            t["mtime"] = mtime
            t["cwd"] = meta.get("cwd") or t["cwd"]
            t["originator"] = meta.get("originator") or t["originator"]
    # 清掉已经不在盘上的文件，别让缓存跟着跑一整天越长越大
    for gone in set(_meta_cache) - live_paths:
        _meta_cache.pop(gone, None)
    return threads


def _threads():
    with _index_lock:
        now = time.time()
        if _index_cache["threads"] is not None and now - _index_cache["at"] < INDEX_TTL:
            return _index_cache["threads"]
        threads = _build_index()
        _index_cache["at"] = now
        _index_cache["threads"] = threads
        return threads


class CodexAdapter(Adapter):
    name = "codex"
    label = "Codex"

    def __init__(self):
        if not os.path.isdir(ROOT):
            raise RuntimeError("找不到 %s" % ROOT)

    # ---------- 内部 ----------

    def _title(self, t):
        """标题取这个线程**第一句**用户说的话。

        从最老那段找，找不到再退最新那段——分页之后最老那段未必还留着开场白。
        按 (文件, mtime) 缓存，列表每 5 秒刷一次，不能每回都重读。
        """
        files = sorted(t["files"], key=os.path.getmtime)
        for path in (files[0], files[-1]):
            key = (path, os.path.getmtime(path))
            hit = _title_cache.get(key)
            if hit is None:
                hit = ""
                for entry in _iter_jsonl(path, max_lines=400):
                    if entry.get("type") != "response_item":
                        continue
                    p = _payload(entry)
                    if p.get("type") != "message" or p.get("role") != "user":
                        continue
                    if not _is_real_user(p):
                        continue
                    text = _clean_display(text_from_content(p.get("content")))
                    if text:
                        hit = text.replace("\n", " ")[:80]
                        break
                _title_cache[key] = hit
            if hit:
                return hit
        return None

    # ---------- 接口 ----------

    def sessions(self, limit=80):
        threads = _threads()
        out = []
        for t in sorted(threads.values(), key=lambda t: t["mtime"], reverse=True):
            if len(out) >= limit:
                break
            # 先截断再算标题：标题要读文件，不该为了一屏之外的几百个线程白读
            out.append({
                "app": self.name,
                "id": t["id"],
                "title": self._title(t) or "(无标题)",
                "cwd": t["cwd"] or "",
                "mtime": t["mtime"],
                "live": False,
                "can_send": self.can_send(t["id"]),
            })
        return out

    def messages(self, sid, limit=400):
        t = _threads().get(sid)
        if not t:
            return []
        out = _merge_segments(t["files"])
        start = max(0, len(out) - limit)
        tail = out[start:]
        # 绝对序号：界面靠它对账新消息。返回的是滑动窗口（最后 limit 条），
        # 光看长度判断"有没有新的"在窗口满了之后会永远判成没有。
        for n, item in enumerate(tail):
            item["i"] = start + n
        return tail

    def can_send(self, sid):
        """能不能发。

        判据是**这个会话是不是 Codex 桌面版开的**——入站走的是桌面版那个
        app-server，终端里跑的（codex-tui / codex-cli）它根本不认识。
        跟窗口开没开**无关**：窗口关着也能发，queue 会把它拉起来跑一轮
        （见 send_note，界面上会说明）。
        """
        t = _threads().get(sid)
        if not t:
            return False
        originator = t.get("originator")
        if IS_WINDOWS:
            return originator in ("codex_work_desktop", "Codex Desktop")
        return originator == "Codex Desktop"

    def blocked_reason(self, sid):
        if self.can_send(sid):
            return None
        if sid not in _threads():
            return "找不到这个 Codex 会话，可能已经被删了"
        return "这个会话是在终端里跑的（不是 Codex 桌面版开的），手机接不进去"

    def send_note(self, sid):
        """能发，但性质得说明白。

        **这不是往一条死记录里追加，是交给 Codex 自己跑一轮**——它会真思考、
        真动工具，跟在电脑上打字没有区别。另外从文件上**分不出**那个会话此刻
        有没有开在电脑上（Codex 没有 Claude 那种注册表），所以窗口关着也会照跑，
        回头把窗口打开就能看到新一轮。
        """
        return ("这条会交给 Codex 自己跑一轮——跟在电脑上打字一样，"
                "它会真的干活。电脑上那个窗口没开也会照跑，回头打开就能看到。")

    def send(self, sid, text):
        if not self.can_send(sid):
            raise RuntimeError(self.blocked_reason(sid) or "发不了")
        codex_bin = _codex_bin()
        if not codex_bin or not os.path.exists(codex_bin):
            raise RuntimeError("找不到可用的 codex 可执行文件（可用 PHONE_BRIDGE_CODEX 指定）")
        run_kwargs = {}
        if IS_WINDOWS:
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                [codex_bin, "queue", "--thread", sid, "--message", text],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                **run_kwargs
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Codex 20 秒没回应，这条可能没发出去")
        except OSError as exc:
            raise RuntimeError("叫不动 codex：%s" % exc)
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        # 退出码和输出都要看：它失败时是把 JSON-RPC 的错误原样打出来，
        # 光看退出码会漏。成功就一行 "Queued message <id> for thread <id>."
        if proc.returncode != 0 or "Queued message" not in out:
            detail = err or out or "退出码 %d" % proc.returncode
            if "state_5.sqlite" in detail and (
                "readonly database" in detail.lower() or "只读" in detail
            ):
                detail += (
                    "\n这通常是 phone-bridge 从受限沙箱启动，导致 codex 子进程"
                    "无法写 ~/.codex/state_5.sqlite。请从普通 PowerShell / 终端"
                    "重新启动 bridge 后再试。"
                )
            raise RuntimeError("Codex 没收下这条：%s" % detail)
        return {"ok": True, "via": "codex queue", "detail": out}
