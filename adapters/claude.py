"""Claude Code / Claude Desktop 适配器。

出站：扫 ~/.claude/projects/<项目>/<sessionId>.jsonl
入站：写会话自己的 UDS 消息通道 /tmp/cc-socks/<pid>.sock
      —— 前提：本进程是那个会话的子孙进程（Skill 启动时天然满足）
"""

import glob
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time

from .base import Adapter, text_from_content

ROOT = os.path.expanduser("~/.claude/projects")
SOCKS_DIR = "/tmp/cc-socks"
FIRST_LINE_TIMEOUT = 10

# 这份 skill 自己的绝对路径。给别的会话的提示里必须用它——那个会话的
# 工作目录不一定是这个项目，相对路径它跑不起来。
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY_CMD = "python3 -u %s --relay" % os.path.join(SKILL_DIR, "bridge.py")

# 「不能发」时给手机的一句实话。
#
# **别把 shell 命令甩给用户。** 起中继这件事，在电脑上跟那个对话说一句话就行
# ——「启动 phone-bridge」会命中 SKILL.md，那边的 agent 自己知道该起中继
# （SKILL.md 里写了：已经有前端在跑就起中继，没有再起前端）。
# 用户要的是"接着说"，不是"看懂一条命令行"。
BLOCKED_NO_RELAY = (
    "电脑上这个对话正开着，但它那边还没起 phone-bridge。"
    "在那个对话里跟它说一句「启动 phone-bridge」，这边就能发了。"
)
BLOCKED_NOT_RUNNING = (
    "这个对话没在跑，手机上只能看。"
    "在电脑上把它打开，再跟它说一句「启动 phone-bridge」，就能发了。"
)

# 「没活进程的对话」怎么发：自己起一个 headless 会话，stdin 就是我们的
# （不需要进程树资格，见 PORTING.md §1.1）。这是**另起一个脑子**，不是接管；
# 它是真 agent，会回话、会动工具，所以闲下来要收掉，别攒一堆。
#
# **默认关掉了。** 实测下来它不值：语义说不圆（"另起一个脑子"不是"接管"）、
# 要偷祖先进程的凭据、会跟桌面窗口抢同一份记录、起的还是个会动工具的真 agent，
# 而它换来的只是"历史对话也能发"这一个边角场景。界面因此只留两类——正在跑且
# 起了中继的（能发）、正在跑但没起中继的（灰的，提示去那边说一句话）；历史对话
# 就只读。代码留着，放出来设 PHONE_BRIDGE_ALLOW_SPAWN=1。
CLAUDE_BIN = os.environ.get("PHONE_BRIDGE_CLAUDE") or shutil.which("claude")
ALLOW_SPAWN = os.environ.get("PHONE_BRIDGE_ALLOW_SPAWN") == "1"
SPAWN_IDLE_TTL = 900
_SPAWNED = {}                       # sid -> {"proc": Popen, "at": 最后一次用的时间}
_SPAWNED_LOCK = threading.RLock()

# 入站中继的落脚点。**故意不是端口**：中继绑的是 UDS 文件，不是地址。
# 手机那边永远只见一个端口（前端的），中继对它不存在——它按 sessionId
# 被自动发现。文件名里带中继自己的 pid，见 _relay_for()。
RELAY_DIR = "/tmp/phone-bridge-relays"
RELAY_TIMEOUT = 15

# 跨会话消息的信封。显示名（from-name）必须由发送方自己声明，
# 不声明的话收件方那边一律显示成「unknown」。
PEER_TAG = "cross-session-message"
PEER_NAME = "phone-bridge"

# 按 harness 的实际解析顺序：from / from-session / hop-chain / from-name / from-mode
_ENVELOPE_RE = re.compile(
    r"^<" + PEER_TAG + r'(?: from="[^"]*")?(?: from-session="[^"]*")?'
    r'(?: hop-chain="[^"]*")?(?: from-name="[^"<>\n\r]*")?'
    r'(?: from-mode="[^"]*")?>\n([\s\S]*)\n</' + PEER_TAG + r">$"
)

# harness 收到跨会话消息后，会在正文前后各糊一段英文。都是给**模型**看的，
# 不该出现在手机界面上。前缀按行剥；后缀用 rfind 从**最后**一处切——
# 用户要是自己贴过一段一模一样的英文，切前面那处会把用户的话一起吃掉。
_PEER_PREFIX = "Another Claude session sent a message:\n"
_PEER_SUFFIX = "This came from another Claude session — not typed by your user"

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
_registry_cache = {"at": 0.0, "map": {}}
_ancestor_cache = {"at": 0.0, "pids": set()}


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _fmt_tokens(n):
    if n >= 10000:
        return "%.1f万" % (n / 10000.0)
    return str(n)


def _registry():
    """sessionId -> harness 写的那条会话登记项。

    **这是权威来源。** pid、cwd、忙/闲状态都是 harness 自己写下来的。

    **别退回去用 `ps` 扫 `--resume=`。** 全新会话的命令行里根本没有
    `--resume=`（没东西可 resume），扫不到，于是刚新建的对话永远是灰的。
    这个坑踩过——而且它只在「刚点新建」这一条路上坏，手上有个聊过几轮的
    老会话时测不出来。

    顺带白捡一个 signal：`status` 是 `idle` / `busy`，直接就是"它在不在干活"，
    不用自己去日志里猜。实测干活时真的会翻成 busy。
    """
    now = time.time()
    if now - _registry_cache["at"] < 2.0:
        return _registry_cache["map"]
    found = {}
    for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(path) as fh:
                info = json.load(fh)
        except (OSError, ValueError):
            continue  # 半写完的文件，下次再看
        pid, sid = info.get("pid"), info.get("sessionId")
        if isinstance(pid, int) and sid and _pid_alive(pid):
            found[sid] = info
    _registry_cache["at"] = now
    _registry_cache["map"] = found
    return found


def _live_claude_procs():
    """sessionId -> pid，只认当前在跑的 claude 进程。

    **要把我们自己自起的那些排除掉。** 自起的 headless 会话也会注册，于是同一个
    sessionId 会对应两个 pid（§1.1 事实 2），按 sid 存就必然挑错一个——挑到自起
    的那个，帧就发给了一个没人看的脑袋。自起的进程发消息走 stdin，不该进这张表。
    """
    mine = _spawned_pids()
    return {
        sid: info["pid"]
        for sid, info in _registry().items()
        if info["pid"] not in mine
    }


def _session_of_ancestor():
    """我待在哪个会话的进程树里？返回 (sessionId, 那个会话的 pid)，没有就是 (None, None)。

    中继模式靠它认出自己该服务谁——不用传参数，也不会认错（一个进程的祖先
    链里最多只有一个会话进程）。

    注意这里是 **pid -> sid**，不是 _live_claude_procs() 那个方向：自起的
    headless 会话也会注册，同一个 sessionId 会有两个 pid，按 sid 反查会静默
    挑错一个（§1.1）。按 pid 查没有这个问题。
    """
    by_pid = {info["pid"]: sid for sid, info in _registry().items()}
    pid = os.getpid()
    for _ in range(20):
        try:
            raw = subprocess.check_output(
                ["ps", "-o", "ppid=", "-p", str(pid)], stderr=subprocess.DEVNULL
            ).strip()
            ppid = int(raw or 0)
        except Exception:
            break
        if ppid <= 1:
            break
        if ppid in by_pid:
            return by_pid[ppid], ppid
        pid = ppid
    return None, None


def _relay_for(sid):
    """这个会话里有没有活着的入站中继？有就返回它的 UDS 路径，没有返回 None。

    跟注册表同一个思路：让写的人把事实写下来（pid 写在文件名里），读的人
    只做 liveness 检查，别去猜。中继死了但 socket 文件还在的那种残骸，
    pid 一查就露馅，顺手清掉。
    """
    hits = glob.glob(os.path.join(RELAY_DIR, "%s.*.sock" % sid))
    for path in sorted(hits, key=os.path.getmtime, reverse=True):
        try:
            rpid = int(os.path.basename(path)[:-len(".sock")].rsplit(".", 1)[1])
        except (IndexError, ValueError):
            continue
        if _pid_alive(rpid):
            return path
        try:
            os.unlink(path)
        except OSError:
            pass
    return None


def _relay_send(sock_path, text):
    """把正文交给住在目标会话树里的中继，让它替我发——只有它有资格。

    正文以**原文**过线，信封由中继那边自己包（包壳是发送方的活，放一处
    就够了）。中继会在回话里告诉我们到底成没成，这比 socket 的返回码可信。
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(RELAY_TIMEOUT)
    buf = b""
    try:
        sock.connect(sock_path)
        sock.sendall((json.dumps({"text": text}, ensure_ascii=False) + "\n").encode("utf-8"))
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        sock.close()
    line = buf.split(b"\n")[0]
    if not line:
        raise RuntimeError("中继没回话（它可能刚退出了）")
    reply = json.loads(line.decode("utf-8"))
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error") or "中继拒绝发送")
    return reply


# ---------- 自起 headless 会话：给「没在跑」的历史对话用 ----------

def _spawned_pids():
    """我们自己拉起那些 headless 会话的 pid，顺手清掉已经死掉的记录。

    这是一份**排除集**。自起的会话照样会注册、照样开 socket（§1.1 事实 2），
    于是同一个 sessionId 会对应两个 pid——不排掉的话，任何按 sid 存的表都会
    静默挑错一个，消息投给了没人看的那一边。
    """
    with _SPAWNED_LOCK:
        for sid in [s for s, r in _SPAWNED.items() if r["proc"].poll() is not None]:
            _SPAWNED.pop(sid, None)   # pid 会被系统复用，死掉的记录别留着误伤别人
        return {r["proc"].pid for r in _SPAWNED.values()}


def _headless_busy(pid):
    """我们自起的那个脑袋现在在出话吗？收它之前得问一句，不然回复会被截断。"""
    for info in _registry().values():
        if info.get("pid") == pid:
            return info.get("status") == "busy"
    return False


def _reap_spawned():
    """闲太久的自起会话收掉，别攒一堆。

    它是**真 agent**，会回话也会动工具，不是一根安静的管子；手机那边聊完就
    不管它了，所以 TTL 按"最后一次用它"算。

    还有一条比 TTL 更急的：**自起之后，用户又把那个对话在电脑上打开了。**

    同一个 sessionId 于是有两个活进程——桌面窗口一个、我们自起的那个一个。
    桌面窗口的上下文在**它自己的内存里**，它不读记录，所以往我们这边说话，
    那个窗口里根本看不到（用户实测：手机上说了话、电脑上那个窗口没有）。
    这时候要**立刻收掉自己这个**，把路权让回桌面：下一轮 _route() 就会把
    这个对话判成"电脑端开着、不能发"，而不是继续往一个看不见的脑袋里灌话。
    也别留着它——留着的后果更阴：用户把窗口一关，下次手机发消息会**复用**
    这个还活着的脑袋，而它的上下文停在打开窗口之前，等于对着一段过期记忆说话。
    """
    now = time.time()
    with _SPAWNED_LOCK:
        for sid, rec in list(_SPAWNED.items()):
            proc = rec["proc"]
            if proc.poll() is not None:
                _SPAWNED.pop(sid, None)
                continue
            # _live_claude_procs() 已经排除了我们自起的 pid，这里还能查到，
            # 说明来了个**外人**（桌面窗口）——它在读同一份记录。
            if _live_claude_procs().get(sid) is not None:
                if _headless_busy(proc.pid):
                    continue          # 正在出话，下轮再说，别把回复切一半
                _SPAWNED.pop(sid, None)
                try:
                    proc.terminate()
                except OSError:
                    pass
                print("[claude] %s 在电脑上被打开了，收掉自起的那个脑袋" % sid[:8])
                continue
            if now - rec["at"] > SPAWN_IDLE_TTL:
                _SPAWNED.pop(sid, None)
                try:
                    proc.terminate()   # SIGTERM：留个机会让它把 transcript 收尾
                except OSError:
                    pass


# 从祖先会话进程身上抄下来的凭据。TTL 短一点：万一它换，我们跟着换。
AUTH_VARS = (
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS",
)
_auth_cache = {"at": 0.0, "env": {}}


def _auth_from_ancestors():
    """往上找一个**会话进程**，把它环境里的凭据读出来。

    为什么非得这样：harness 会把凭据从**所有子进程**的环境里剥干净
    （实测：前端和我自己都只剩 `ANTHROPIC_BASE_URL`，token 没了）。于是自起的
    headless 会话一律 `Not logged in`——它起得来、socket 也注册，就是出不了话，
    而且失败会**当成一条回复写进那个对话**，凭空污染用户的历史记录。
    真正能用的凭据只活在会话进程自己的环境里，同机器同用户，只能去那儿拿。

    实测过的两条路，只有这条通：
      - `settings.json` 里的 `ANTHROPIC_API_KEY` → 401（这个自建网关要
        `Authorization` 头，不吃 `x-api-key`）；
      - 会话进程的 `ANTHROPIC_AUTH_TOKEN` → 正常出话。

    值只往子进程的环境里传：不落盘、不打印、不出这台机器。
    `PHONE_BRIDGE_INHERIT_AUTH=0` 可以关掉——关掉之后就只有"有活进程的对话"
    能写了。
    """
    if os.environ.get("PHONE_BRIDGE_INHERIT_AUTH") == "0":
        return {}
    now = time.time()
    if now - _auth_cache["at"] < 60:
        return _auth_cache["env"]
    pid, found = os.getpid(), {}
    for _ in range(20):
        try:
            ppid = int(subprocess.check_output(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                stderr=subprocess.DEVNULL,
            ).strip() or 0)
        except Exception:
            break
        if ppid <= 1:
            break
        try:
            raw = subprocess.check_output(
                ["ps", "eww", "-p", str(ppid)], stderr=subprocess.DEVNULL
            ).decode("utf-8", "replace")
        except Exception:
            raw = ""
        hit = {}
        for name in AUTH_VARS:
            m = re.search(r"(?:^|\s)" + name + r"=(\S+)", raw)
            if m:
                hit[name] = m.group(1)
        if hit.get("ANTHROPIC_AUTH_TOKEN") or hit.get("ANTHROPIC_API_KEY"):
            found = hit
            break
        pid = ppid
    _auth_cache["at"] = now
    _auth_cache["env"] = found
    return found


def _spawn_proc(sid, cwd):
    """拉起一个 headless 会话。**调用方必须持有 _SPAWNED_LOCK。**

    stdin 是我们的，所以没有树认证这回事——这正是它存在的理由（§1.1）。
    stdout 直接丢掉：回话从 transcript 里读，跟别处一视同仁。
    """
    proc = subprocess.Popen(
        [
            CLAUDE_BIN, "--resume", sid,
            "--input-format", "stream-json",
            # --verbose 不是可选的：不给它，stream-json 输出会被直接拒掉
            # （`--output-format=stream-json requires --verbose`，进程当场退出，
            # 而且只留下两行 init 元数据——看起来"发送成功"，其实什么都没进去）。
            "--output-format", "stream-json", "--verbose",
        ],
        cwd=cwd or None,          # 那个对话自己的目录，别继承前端的
        env={**os.environ, **_auth_from_ancestors()},
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {"proc": proc, "at": time.time()}


def _spawn_send(sid, cwd, text):
    """没活进程的对话：自己起一个 headless 会话，把话写进它的 stdin。

    路是干净的（不用认证、不用逆向、想开几个开几个），但**它不是接管**，
    三件事必须先说清楚：

    - 这是**另起一个脑子**：真 agent，会回话、会动工具。
    - 它跟桌面窗口抢**同一份 transcript**（事实 1），所以只在"这个对话没在跑"
      时才走这条路；有活进程一律优先注入/中继（选通道见 §10）。
    - 权限提示没人接（stdout 丢弃了），要授权的工具那一轮会被拒或卡住。
    """
    if not CLAUDE_BIN:
        raise RuntimeError("找不到 claude 可执行文件（用 PHONE_BRIDGE_CLAUDE 指一个）")
    if not _auth_from_ancestors():
        # 宁可不发，也别发：没凭据的自起会话会回一句 "Not logged in"，
        # 而那句话是**当成一条回复写进用户对话里的**，等于拿垃圾污染他的历史。
        raise RuntimeError(
            "拿不到凭据，自起的会话会登录失败（harness 会把子进程的 token 剥掉；"
            "从上层会话进程里也没读到）。检查 PHONE_BRIDGE_INHERIT_AUTH 是不是 0。"
        )
    _reap_spawned()
    with _SPAWNED_LOCK:
        rec = _SPAWNED.get(sid)
        if rec is None or rec["proc"].poll() is not None:
            rec = _spawn_proc(sid, cwd)
            _SPAWNED[sid] = rec
        rec["at"] = time.time()
        proc = rec["proc"]
    # **这条路不套信封**，跟注入/中继不一样。信封是给"写进别人的桌面会话"用的
    # ——那边的宿主 UI 靠它署名、靠它把这句标成"来自外部注入"。自起的会话没有
    # 别的宿主 UI，套上去只会让**模型**以为这是"另一个会话发来的消息"。
    # 实测踩过：那个 agent 于是转而用跨会话工具去**回复发送方**，回复落到了另一个
    # 会话里，而用户手机上那一页看不到这条往来。用户自己打的话，就该是一条普通的
    # 用户消息。
    frame = {"type": "user", "message": {"role": "user", "content": text}}
    try:
        proc.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
        proc.stdin.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        with _SPAWNED_LOCK:
            _SPAWNED.pop(sid, None)
        raise RuntimeError("自起的会话写不进去（%s），这条丢了" % exc)
    return {"ok": True, "via": "spawn"}


def _my_ancestors():
    """我自己的祖先 pid 集合。

    入站通道只接受「会话进程的子孙」——这条路是靠进程树关系认证的。
    bridge 一旦被 reparent 到 launchd（nohup ... & 就会），
    发过去的帧会被**静默丢弃**，socket 照样 accept、照样返回 ok。
    所以必须自己查清楚，否则 can_send 会说谎。
    """
    now = time.time()
    if now - _ancestor_cache["at"] < 2.0:
        return _ancestor_cache["pids"]
    out, pid = set(), os.getpid()
    for _ in range(20):
        try:
            raw = subprocess.check_output(
                ["ps", "-o", "ppid=", "-p", str(pid)], stderr=subprocess.DEVNULL
            ).strip()
            ppid = int(raw or 0)
        except Exception:
            break
        if ppid <= 1:
            break
        out.add(ppid)
        pid = ppid
    _ancestor_cache["at"] = now
    _ancestor_cache["pids"] = out
    return out


def _can_reach(session_pid):
    """会话进程得是我的祖先，注入才不会被丢。"""
    return session_pid in _my_ancestors()


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


def _is_tool_result_only(entry):
    msg = entry.get("message") or {}
    if entry.get("toolUseResult") is not None:
        return True
    content = msg.get("content")
    if isinstance(content, list):
        kinds = [b.get("type") for b in content if isinstance(b, dict)]
        if kinds and all(k == "tool_result" for k in kinds):
            return True
    return False


def _strip_envelope(text):
    """把 harness 套的壳剥掉，只留人打的那句话。

    一段跨会话消息在日志里长这样：

        Another Claude session sent a message:
        <cross-session-message from-name="phone-bridge">
        正文
        </cross-session-message>

        This came from another Claude session — not typed by your user, …

    前后两段是 harness 加的，中间的信封是我们自己加的——都是给**模型**看的。
    手机界面显示的是用户自己刚发出去的话，糊上三层包装没法看。
    （模型那边照样收到完整的一段，这里只动显示。）认不出来就原样返回。

    形状不对也要能活：调用方可能递进来 content-block 列表（`attachment.prompt`
    就是这种形状，带图片附件时尤其如此），渲染器不该因为这个整页 500。
    """
    if not isinstance(text, str):
        text = text_from_content(text)
    if text.startswith(_PEER_PREFIX):
        text = text[len(_PEER_PREFIX):]
    cut = text.rfind(_PEER_SUFFIX)
    if cut != -1:
        text = text[:cut]
    text = text.strip()
    m = _ENVELOPE_RE.match(text)
    return m.group(1) if m else text


def _envelope(text):
    """把正文包成 harness 认得的跨会话信封。

    包这一层只为了署名：显示名得由发送方在信封里声明，不声明的话
    收件方那边一律是「unknown」。包错了也不会更糟——正则认不出来
    就退回普通文本处理，跟以前完全一样。

    正文不做转义。解析用的是贪婪匹配 + 行尾锚定，正文里就算出现
    标签名也会被正确切出来（试过）；转义反而会悄悄吃掉用户打的字。
    """
    return "<%s from-name=\"%s\">\n%s\n</%s>" % (PEER_TAG, PEER_NAME, text, PEER_TAG)


_TASK_NOTIFY_RE = re.compile(r"<task-notification>([\s\S]*)</task-notification>")

# 正文自己就长得像系统块的（实测有 origin 为 null 的，光看 origin 会漏一大半）
_SYSTEM_TAGS = ("<task-notification>",)


def _note_text(text):
    """把一段系统块压成一行。

    比如后台任务结束的通知，原文是 5000 多字的 XML。原样渲染就是一大坨 XML
    糊在**用户自己的蓝色气泡**里——看着像用户打了一段机器话。
    """
    m = _TASK_NOTIFY_RE.search(text)
    body = m.group(1) if m else text
    status = re.search(r"<status>([^<]*)</status>", body)
    summary = re.search(r"<summary>([\s\S]*?)</summary>", body)
    bits = ["系统通知"]
    if status:
        s = status.group(1).strip()
        bits.append({"completed": "已完成", "failed": "失败"}.get(s, s))
    if summary:
        bits.append(summary.group(1).strip().replace("\n", " "))
    if len(bits) == 1:  # 什么都没解析出来，至少别把原文整个丢进去
        bits.append(text.strip().replace("\n", " ")[:120])
    return " · ".join(bits)


def _system_note(origin, text):
    """系统自己塞的吗？是就返回压好的一行，不是返回 None。

    **两个判据都得看**，只用第一个会漏：
    - `origin.kind` 不是 peer/human —— harness 标的，但**经常会缺**
      （实测 7 条后台任务通知里 6 条 `origin` 是 `null`）
    - 正文自己就是一段系统块 —— 内容自己说了自己是什么，最可靠
    """
    okind = (origin or {}).get("kind")
    if okind and okind not in ("peer", "human"):
        return _note_text(text)
    if not isinstance(text, str):
        text = text_from_content(text)
    if text.lstrip().startswith(_SYSTEM_TAGS):
        return _note_text(text)
    return None


def _compact_text(meta):
    """压缩边界那一行的人话。字段缺了就少说，别编。"""
    bits = ["手动压缩上下文" if meta.get("trigger") == "manual" else "自动压缩上下文"]
    pre, post = meta.get("preTokens"), meta.get("postTokens")
    if isinstance(pre, int) and isinstance(post, int):
        bits.append("%s → %s 字" % (_fmt_tokens(pre), _fmt_tokens(post)))
    ms = meta.get("durationMs")
    if isinstance(ms, int):
        bits.append("用了 %d 秒" % round(ms / 1000.0))
    dropped = meta.get("cumulativeDroppedTokens")
    if isinstance(dropped, int) and dropped:
        bits.append("这一路累计丢掉 %s 字" % _fmt_tokens(dropped))
    return " · ".join(bits)


class ClaudeAdapter(Adapter):
    name = "claude"
    label = "Claude"
    relay_dir = RELAY_DIR

    def __init__(self):
        if not os.path.isdir(ROOT):
            raise RuntimeError("找不到 %s" % ROOT)

    # ---------- 内部 ----------

    def _path_for(self, sid):
        hits = glob.glob(os.path.join(ROOT, "*", "%s.jsonl" % sid))
        if not hits:
            return None
        return max(hits, key=os.path.getmtime)

    def _summarize(self, path):
        """只读文件头部，拿到标题 / cwd，避免每个会话都全量解析。"""
        title, cwd = None, None
        last_prompt = None
        for entry in _iter_jsonl(path, max_lines=400):
            etype = entry.get("type")
            if cwd is None and entry.get("cwd"):
                cwd = entry["cwd"]
            if etype == "custom-title" and entry.get("title"):
                title = entry["title"]
            elif etype == "last-prompt" and entry.get("prompt"):
                last_prompt = entry["prompt"]
            elif etype == "user" and title is None and not _is_tool_result_only(entry):
                text = _strip_envelope(
                    text_from_content((entry.get("message") or {}).get("content"))
                )
                if text.strip():
                    title = text.strip().replace("\n", " ")[:80]
        return title or last_prompt or "(无标题)", cwd

    # ---------- 接口 ----------

    def sessions(self, limit=80):
        paths = sorted(
            glob.glob(os.path.join(ROOT, "*", "*.jsonl")),
            key=os.path.getmtime,
            reverse=True,
        )[: limit * 2]
        reg = _registry()
        out = []
        for path in paths[:limit]:
            sid = os.path.splitext(os.path.basename(path))[0]
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            title, cwd = self._summarize(path)
            info = reg.get(sid) or {}
            out.append({
                "app": self.name,
                "id": sid,
                "title": title,
                "cwd": cwd or "",
                "mtime": mtime,
                "live": bool(info),
                "busy": info.get("status") == "busy",
                "can_send": self.can_send(sid),
            })
        return out

    def is_busy(self, sid):
        info = _registry().get(sid)
        return None if not info else info.get("status") == "busy"

    def messages(self, sid, limit=400):
        path = self._path_for(sid)
        if not path:
            return []
        out = []
        for entry in _iter_jsonl(path):
            etype = entry.get("type")

            # 上下文压缩：事后落一条边界记录，带真实数字（触发方式、前后 token、
            # 耗时）。注意它**是压缩完了才写**的——压缩过程中日志一动不动，
            # 所以那 30 秒只有「忙」可看，别假装能实时显示"正在压缩"。
            if etype == "system" and entry.get("subtype") == "compact_boundary":
                out.append({
                    "role": "system",
                    "text": _compact_text(entry.get("compactMetadata") or {}),
                    "ts": entry.get("timestamp"),
                    "via": "compaction",
                })
                continue

            # 外部注入进来的消息（UDS / 跨会话）落在 attachment 里
            if etype == "attachment":
                att = entry.get("attachment") or {}
                if att.get("type") != "queued_command":
                    continue
                # prompt 的形状不固定：注入的跨会话消息是字符串，排队进来的
                # 用户输入可能是 content-block 列表（带图时就是）。统一走
                # text_from_content，别假设它一定是 str。
                text = _strip_envelope(text_from_content(att.get("prompt")))
                if not text.strip():
                    continue
                origin = att.get("origin") or {}
                note = _system_note(origin, text)
                if note is not None:
                    out.append({
                        "role": "system",
                        "text": note,
                        "ts": entry.get("timestamp"),
                        "via": "note",
                    })
                    continue
                out.append({
                    "role": "user",
                    "text": text,
                    "ts": entry.get("timestamp"),
                    "via": "peer" if origin.get("kind") == "peer" else "queued",
                })
                continue

            if etype not in ("user", "assistant"):
                continue
            if etype == "user" and _is_tool_result_only(entry):
                continue
            msg = entry.get("message") or {}
            text = _strip_envelope(text_from_content(msg.get("content")))
            if not text.strip():
                continue
            origin = entry.get("origin") or {}
            note = _system_note(origin, text)
            if note is not None:
                # 系统自己塞的（后台任务结束通知之类），不是人打的。当一行说明，
                # 别当气泡——它既不是用户说的，也不是模型说的。
                out.append({
                    "role": "system",
                    "text": note,
                    "ts": entry.get("timestamp"),
                    "via": "note",
                })
                continue
            via = "peer" if origin.get("kind") == "peer" else None
            if entry.get("isCompactSummary"):
                via = "summary"  # 压缩摘要不是用户打的，别当气泡显示
            out.append({
                "role": msg.get("role") or etype,
                "text": text,
                "ts": entry.get("timestamp"),
                "via": via,
            })
        start = max(0, len(out) - limit)
        tail = out[start:]
        # 每条带上它在**完整列表**里的绝对序号。界面那边靠它对账"哪几条是新的"：
        # 这里返回的是最后 limit 条（滑动窗口），长度一到上限就不再增长，
        # 新旧之间下标整体平移——界面只按"长度涨没涨"判断新消息，会永远判成
        # 没有，从此停止更新（现象很像"手机发的话被吃掉了"）。
        for n, item in enumerate(tail):
            item["i"] = start + n
        return tail

    def _route(self, sid):
        """(怎么发, 发到哪, 不能发的原因)。

        两条路，按「离那个对话有多近」排：

        - `("tree", <会话 socket>)`：我自己就是那个会话的子孙，直接注入。
          只对**前端所在的那一个会话**成立（一个进程只有一个父进程）。
        - `("relay", <中继 UDS>)`：那个会话里有我的同伙——它住在**那边的**树里，
          资格是它的，我借它的手发。这就是「一个端口管住所有会话」的全部秘密：
          前端不需要长出无数个端口，中继也**不占端口**，它只是个 UDS 文件，
          按 sessionId 自动被发现。

        两条都不成立就是「不能发」，**而且明说原因**：正在跑但没起中继的，让
        用户去那个对话里说一句话；没在跑的，让他先把它打开。别静默失败——
        harness 会把帧悄悄丢掉，界面上显示"能发"而消息石沉大海，是这个 skill
        早期最难查的一个 bug（见 PORTING.md §2）。

        第三条路（自起 headless 会话）默认关掉了，见文件头的 ALLOW_SPAWN。
        """
        pid = _live_claude_procs().get(sid)
        if pid is None:
            if ALLOW_SPAWN:
                return "spawn", None, None
            return None, None, BLOCKED_NOT_RUNNING
        if _can_reach(pid):
            sock_path = os.path.join(SOCKS_DIR, "%d.sock" % pid)
            if os.path.exists(sock_path):
                return "tree", sock_path, None
            return None, None, "会话没有消息通道（%s）" % sock_path
        relay = _relay_for(sid)
        if relay:
            return "relay", relay, None
        return None, None, BLOCKED_NO_RELAY

    def send_note(self, sid):
        """能发、但发出去的性质跟普通注入不一样时，给界面一句实话。

        只有自起（`ALLOW_SPAWN`，默认关）才不是普通注入，所以平时这里恒返回
        None——留着是因为那个开关一开，这句话就还得说。
        """
        kind, _, _ = self._route(sid)
        if kind == "spawn":
            return ("这个对话没在跑：发过去会另起一个 agent 接着聊（它会真的干活）。"
                    "这期间别在电脑上又把它打开——那就成了两个脑袋，"
                    "电脑那个窗口看不到这边说的话。")
        return None

    def reap(self):
        """定期家务：收掉闲太久的自起会话。"""
        _reap_spawned()

    def _channel(self, sid):
        kind, _, why = self._route(sid)
        return (kind is not None), why

    def can_send(self, sid):
        return self._channel(sid)[0]

    def blocked_reason(self, sid):
        return self._channel(sid)[1]

    def own_session(self):
        """我自己待在哪个会话的树里——中继模式用它认出该服务谁。"""
        return _session_of_ancestor()

    def send(self, sid, text):
        kind, target, why = self._route(sid)
        if kind is None:
            raise RuntimeError(why)
        if kind == "relay":
            return _relay_send(target, text)
        if kind == "spawn":
            path = self._path_for(sid)
            if not path:
                raise RuntimeError("找不到 %s 的会话日志，拉不起来" % sid)
            _, cwd = self._summarize(path)
            return _spawn_send(sid, cwd, text)
        frame = {"type": "user", "message": {"content": _envelope(text)}}
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(FIRST_LINE_TIMEOUT)
        try:
            sock.connect(target)
            sock.sendall((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
            time.sleep(0.2)
        finally:
            sock.close()
        return {"ok": True, "via": "uds"}
