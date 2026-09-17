#!/usr/bin/env python3
"""phone-bridge —— 把本机跑着的 AI 会话接到手机浏览器上。

出站：各 App 适配器读自己的会话日志
入站：各 App 适配器写回自己的通道

启动（务必从会话内部启动，入站依赖进程树关系）：
    python3 bridge.py --port 8971
"""

import argparse
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from adapters import load_adapters  # noqa: E402

ADAPTERS = {}
ADAPTERS_LOCK = threading.Lock()
UI_PATH = os.path.join(HERE, "ui.html")
WATCH_INTERVAL = 5
RELAY_TIMEOUT = 15
HOUSEKEEPING_INTERVAL = 60

# 每个 App 在列表里最多露几条（见 _all_sessions）。**是每个 App 各 10 条**，
# 不是总共 10 条——两个 App 混排，所以一屏最多 20 条。
PER_APP_LIMIT = 10

# 启动时把二维码存这儿，给 agent 拿去发给用户（见 _print_qr）。放临时目录是因为
# 它本来就是个一次性的东西：重启一次覆盖一次，重启电脑就没了，不需要收拾。
_DEFAULT_QR_PNG = (
    os.path.join(tempfile.gettempdir(), "phone-bridge-qr.png")
    if os.name == "nt" else "/tmp/phone-bridge-qr.png"
)
QR_PNG_PATH = os.environ.get("PHONE_BRIDGE_QR_PNG") or _DEFAULT_QR_PNG

# 赞助位（列表页两组之间那一条）。**内容不在这个文件里**——改 GitHub 上仓库根目录的
# ads.json 就行，不用碰这台机器，也不用重启。前端每 ADS_TTL 秒去拉一次。
#
# 拉不到不算错：没网、被墙、json 写坏了，一律退回技能目录里自带的 ads.json；
# 连那份也没有就返回空。**广告永远不该把功能弄挂。**
ADS_URL = os.environ.get("PHONE_BRIDGE_ADS_URL") or (
    "https://raw.githubusercontent.com/bang919/phone-bridge/main/ads.json"
)
ADS_TTL = 300
ADS_PATH = os.path.join(HERE, "ads.json")
_ADS_CACHE = {"at": 0.0, "list": None}
_ADS_LOCK = threading.Lock()


def _clean_ads(raw):
    """挑出能用的条目。

    `name` 必须有；`url` **可以没有**——没有就是纯文字一条（"广告位招租，微信…"
    这种），界面上不画成链接。有的话**只认 http(s)**：它会被塞进 <a href>，
    一个 `javascript:` 就能在手机上执行脚本，而这份内容来自网络。
    url 写坏了只丢 url、不丢整条——内容还在，总比整条消失强。
    """
    out = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            url = ""
        out.append({
            "name": name,
            "desc": str(item.get("desc") or "").strip(),
            "url": url,
        })
    return out


def _read_ads_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return _clean_ads(json.load(fh).get("ads"))
    except Exception:
        return []


def load_ads():
    with _ADS_LOCK:
        now = time.time()
        if _ADS_CACHE["list"] is not None and now - _ADS_CACHE["at"] < ADS_TTL:
            return _ADS_CACHE["list"]
        out = []
        try:
            with urllib.request.urlopen(ADS_URL, timeout=5) as fh:
                out = _clean_ads(json.loads(fh.read().decode("utf-8")).get("ads"))
        except Exception as exc:
            print("[bridge] 拉赞助位失败（用本地那份）：%s" % exc)
        if not out:
            out = _read_ads_file(ADS_PATH)
        # **不管成没成，都记上时间。** 否则网络不通时每个请求都要等满 5 秒超时——
        # 广告拿不到就拿不到，5 分钟后再试，不能拖累页面。
        _ADS_CACHE["at"] = now
        _ADS_CACHE["list"] = out
        return out


def tailscale_ip():
    """Tailscale 那张网卡的 IPv4。没有就返回 None。

    **三个平台的路径都得试。** 早先只写了 macOS 那个 .app 里的二进制，于是
    Windows 上明明装了、也登录了，照样查不到——直接退回只绑回环，手机连不上，
    而横幅只说一句"没找到 Tailscale IP"，看不出是这个原因。
    """
    cands = [
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",  # macOS 桌面版
        "/usr/bin/tailscale",                                    # Linux 包管理器
        "/usr/local/bin/tailscale",
    ]
    if os.name == "nt":
        # 默认安装目录；也覆盖用户改了 ProgramFiles / 装到用户目录的情况。
        cands.extend((
            os.path.join(
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                "Tailscale", "tailscale.exe",
            ),
            os.path.join(
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                "Tailscale", "tailscale.exe",
            ),
            os.path.expandvars(r"%LOCALAPPDATA%\Tailscale\tailscale.exe"),
        ))
    for name in ("tailscale", "tailscale.exe"):
        found = shutil.which(name)
        if found:
            cands.append(found)
    for cand in cands:
        try:
            out = subprocess.check_output(
                [cand, "ip", "-4"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            continue
        # 多网卡时会连着吐好几行，挑第一个像 IPv4 的
        for line in out.decode(errors="replace").splitlines():
            ip = line.strip()
            if ip and ip.count(".") == 3:
                return ip

    # Windows 的 tailscaled 用受保护的命名管道，普通权限进程调用
    # `tailscale ip -4` 可能直接被拒绝。网卡地址仍然能通过本机 DNS
    # 解析拿到，所以 CLI 失败时再按 Tailscale 的 100.64.0.0/10 段找一次。
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        }
    except Exception:
        return None
    for ip in sorted(addresses):
        try:
            if ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10"):
                return ip
        except ValueError:
            continue
    return None


def lan_ip():
    """本机在局域网里的 IPv4。没有就返回 None。

    **得问两个来源，单问一个会错。** UDP 路由探测只认默认路由挑出来的那张网卡，
    而这台机器上装了 Clash（TUN 模式）：默认路由指向那个虚拟网卡，探出来是
    `198.18.0.1`——代理自己编的地址，手机根本够不着。主机名解析给的才是真网卡
    （实测这台机器上是 `192.168.10.176`）。两边都收，按"像不像一个正经的局域网
    地址"排个序再挑。

    那几个 UDP 探测**一个包都不往外发**：UDP 没有握手，`connect` 只是让内核按路由
    表挑一张网卡、记下源地址，然后问它自己。8.8.8.8 只是借来当个"一定在远端的地
    址"，没网也照样能挑出默认网卡。
    """
    cands = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            cands.append(info[4][0])
    except Exception:
        pass
    for probe in (("8.8.8.8", 80), ("1.1.1.1", 80)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(probe)
            cands.append(sock.getsockname()[0])
        except Exception:
            pass
        finally:
            sock.close()

    def rank(ip):
        """越小越优先；`None` 表示这地址不可能是局域网，扔掉。"""
        if ip.count(".") != 3 or ip.startswith(("127.", "169.254.", "0.", "255.")):
            return None
        if ip.startswith(("198.18.", "198.19.")):   # 基准测试保留段，代理拿它当假地址
            return None
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31:
            return 2
        return 3                                     # 兜底，含 Tailscale 的 100.64/10

    usable = [c for c in cands if rank(c) is not None]
    return min(usable, key=rank) if usable else None


def get_adapter(name):
    with ADAPTERS_LOCK:
        ad = ADAPTERS.get(name)
    if ad is None:
        raise KeyError("没有这个 App 的适配器: %s" % name)
    return ad


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "phone-bridge/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[bridge] %s - %s\n" % (self.address_string(), fmt % args))

    # ---------- 工具 ----------

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}

    def _note(self, ad, sid):
        """能发，但发出去的性质跟普通注入不一样时的一句实话。"""
        fn = getattr(ad, "send_note", None)
        if not fn:
            return None
        try:
            return fn(sid)
        except Exception:
            return None

    # ---------- 路由 ----------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                with open(UI_PATH, "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")

            if path == "/api/sessions":
                return self._send(200, {"sessions": self._all_sessions()})

            if path == "/api/ads":
                return self._send(200, {"ads": load_ads()})

            if path == "/api/messages":
                app = (query.get("app") or [""])[0]
                sid = (query.get("sid") or [""])[0]
                if not app or not sid:
                    return self._send(400, {"error": "缺少 app / sid"})
                ad = get_adapter(app)
                msgs = ad.messages(sid)
                return self._send(200, {
                    "messages": msgs,
                    "can_send": ad.can_send(sid),
                    "blocked": ad.blocked_reason(sid),
                    "note": self._note(ad, sid),
                    "busy": ad.is_busy(sid),
                })

            return self._send(404, {"error": "没有这个路径"})
        except KeyError as exc:
            return self._send(404, {"error": str(exc)})
        except Exception as exc:
            return self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/send":
            return self._send(404, {"error": "没有这个路径"})
        body = self._json_body()
        app = body.get("app") or ""
        sid = body.get("sid") or ""
        text = (body.get("text") or "").strip()
        if not app or not sid or not text:
            return self._send(400, {"error": "缺少 app / sid / text"})
        try:
            ad = get_adapter(app)
            result = ad.send(sid, text)
            return self._send(200, result or {"ok": True})
        except KeyError as exc:
            return self._send(404, {"error": str(exc)})
        except Exception as exc:
            return self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

    # ---------- 数据 ----------

    def _all_sessions(self):
        # **每个 App 最多 10 条**。手机上要的是"最近在说的那几件事"，不是全部
        # 历史：Codex 盘上 877 个线程、光最近就 80 条，一屏刷不到头，想找"能说话
        # 的那个"得翻半天。按最后活动时间取前 N 条，够用。想看更旧的，去电脑上。
        out = []
        for name, ad in ADAPTERS.items():
            rows = []
            try:
                for s in ad.sessions():
                    if not s.get("can_send"):
                        try:
                            s["blocked"] = ad.blocked_reason(s.get("id"))
                        except Exception:
                            s["blocked"] = None
                    rows.append(s)
            except Exception as exc:
                print("[bridge] %s.sessions() 出错: %s" % (name, exc))
            rows.sort(key=lambda s: s.get("mtime") or 0, reverse=True)
            out.extend(rows[:PER_APP_LIMIT])
        out.sort(key=lambda s: s.get("mtime") or 0, reverse=True)
        return out


def _watch_session(stop_event):
    """会话没了，自己走。

    子进程不会随父进程一起死：父进程一没，它会被 launchd 收养，然后一直
    跑下去（这个坑踩过——一个被 nohup 起的 bridge 在启动者消失后还活着）。
    所以得自己盯：发现被收养了就说明会话结束了，自己了断。
    """
    if os.name == "nt":
        # Windows 没有 launchd 那种 reparent；父 pid 也可能在服务启动后立刻变化。
        # 前台服务只由自己退出/被打断来结束，别拿父进程关系误杀它。
        return
    start_ppid = os.getppid()
    if start_ppid == 1:
        print("[bridge] 警告：启动时父进程就是 launchd（孤儿）")
        print("[bridge] 这种状态下入站注入不会生效，也不会自动退出")
        return
    while not stop_event.wait(WATCH_INTERVAL):
        if os.getppid() != start_ppid:
            print("[bridge] 会话进程没了，bridge 跟着退出")
            stop_event.set()
            return


def _housekeeping(stop_event):
    """定期让各适配器做家务。

    目前只有一件事：回收那些还活着的临时进程。claude 的自起功能默认关着
    （`PHONE_BRIDGE_ALLOW_SPAWN`），关着的时候这里等于空转——但那个开关一开，
    它拉起的都是**真 agent**，不能攒着不收拾，所以线程照常跑。
    """
    while not stop_event.wait(HOUSEKEEPING_INTERVAL):
        for ad in list(ADAPTERS.values()):
            fn = getattr(ad, "reap", None)
            if not fn:
                continue
            try:
                fn()
            except Exception as exc:
                print("[bridge] %s.reap() 出错: %s" % (ad.name, exc))


def _relay_handle(conn, ad, sid):
    """一条连接 = 一条消息：注入我所在的会话，再把结果回给前端。"""
    reply = {"ok": False, "error": "中继内部错误"}
    try:
        conn.settimeout(RELAY_TIMEOUT)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        req = json.loads(buf.split(b"\n")[0].decode("utf-8"))
        text = (req.get("text") or "").strip()
        if not text:
            raise ValueError("空消息")
        reply = {"ok": True, "via": "relay", "detail": ad.send(sid, text)}
    except Exception as exc:
        reply = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    try:
        conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception:
        pass
    finally:
        conn.close()


def run_relay(adapter_name):
    """中继模式：住在一个会话的进程树里，替前端把消息注入进去。

    **它不绑端口。** 只在 <relay_dir>/<sid>.<pid>.sock 上听。资格是进程树
    给的（一个中继只对一个会话有效），所以每个想被写入的桌面会话各要一个
    ——但手机那边永远只有一个地址：前端那个端口。前端按 sessionId 自动
    发现这里，用户不需要知道任何端口。

    必须**从那个会话内部**起（在那边以后台任务方式跑这条命令），否则它
    自己就不在会话的树里，中继等于白起。
    """
    if os.name == "nt":
        print("[relay] Windows 通过命名管道 + token 直接发送，不需要中继")
        return 0
    ad = get_adapter(adapter_name)
    relay_dir = getattr(ad, "relay_dir", None)
    if not relay_dir:
        print("[relay] %s 适配器不支持中继" % adapter_name)
        return 1
    sid, _session_pid = ad.own_session()
    if not sid:
        print("[relay] 没找到我所在的会话——父进程是不是已经是 launchd 了？")
        print("[relay] 中继必须从会话内部起（在那个会话里以后台任务方式跑这条命令）")
        return 1
    os.makedirs(relay_dir, 0o700, exist_ok=True)
    path = os.path.join(relay_dir, "%s.%d.sock" % (sid, os.getpid()))
    if os.path.exists(path):
        os.unlink(path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)
    srv.settimeout(1.0)

    print("")
    print("  phone-bridge 中继已就绪（不占端口）")
    print("    会话: %s" % sid)
    print("    通道: %s" % path)
    print("")
    print("  前端（那个绑端口的）会自动发现它，手机那边不用配任何东西。")

    stop_event = threading.Event()
    threading.Thread(target=_watch_session, args=(stop_event,), daemon=True).start()
    try:
        while not stop_event.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=_relay_handle, args=(conn, ad, sid), daemon=True
            ).start()
    finally:
        try:
            srv.close()
        except Exception:
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
    return 0


def _qr_png_bytes(matrix, scale=10, quiet=2):
    """把二维码矩阵写成灰度 PNG。**纯标准库**，不引 Pillow / pypng。

    `qrcode` 自带的 `PyPNGImage` 后端看着是"纯 python"，实际它还要额外的
    `pypng` 包——这台机器上没装，报 ImportError。与其再挂一个大概率也会缺的
    可选依赖，不如自己拼：PNG 的灰度格式简单到十几行就够，这条路上没有
    "装没装"这个变量。

    `quiet` 是四周再补几格空白。矩阵自己已经带了 `border=2`，这里补到 4 格
    ——规范要求的最小静默区。扫码器靠它把码和背景分开。
    """
    import struct
    import zlib

    n = len(matrix)
    size = (n + quiet * 2) * scale
    white_row = b"\x00" + b"\xff" * size
    rows = [white_row] * (quiet * scale)
    for row in matrix:
        line = bytearray([0])          # 每行开头的 filter 字节，0 = 不过滤
        edge = bytes([255]) * (quiet * scale)
        px = bytearray()
        for cell in row:
            px += bytes([0 if cell else 255]) * scale
        line += edge + bytes(px) + edge
        rows.extend([bytes(line)] * scale)
    rows.extend([white_row] * (quiet * scale))
    raw = b"".join(rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _ensure_qrcode():
    """把 `qrcode` 弄到手，拿不到返回 None。

    **不能"没装就算了"。** 用户要的就是"起完给我一张能扫的图"——静悄悄少一张图，
    在他那边看到的就是"这个功能坏了"，而真正的原因躺在后台任务的日志里，他看不到。

    所以没装就当场装一个（`--user`，装进用户目录，不动系统环境）。装之前会先
    说一声，不搞小动作。装不上才认输，并且把确切命令打出来——这时候横幅是**明说
    "二维码没有"**，不是一带而过。
    """
    try:
        import qrcode
        return qrcode
    except ImportError:
        pass
    print("  [bridge] 没装 `qrcode`，正在装（pip install --user qrcode）…")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "--quiet", "qrcode"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        if proc.returncode:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            if detail:
                print("  [bridge] pip: %s" % detail[-1])
            else:
                print("  [bridge] pip 退出码：%d" % proc.returncode)
            raise RuntimeError("pip install 返回 %d" % proc.returncode)
        import importlib
        importlib.invalidate_caches()
        import qrcode
        return qrcode
    except Exception as exc:
        print("  [bridge] 二维码出不来：装不上 `qrcode`（%s）" % exc)
        print("  [bridge] 自己装一下再重启：%s -m pip install qrcode" % sys.executable)
        return None


def _print_qr(url):
    """把地址画成二维码，手机相机扫一下就进去，不用在手打 IP。

    **只给远端地址画**（`127.0.0.1` 是电脑自己连自己用的，手机扫了打不开）。
    调用方保证传进来的不是回环地址——真的一个远端地址都没有时，横幅会明说
    "手机连不上"，而不是画一张扫了也没用的码。

    颜色是**写死的 ANSI**（白底黑格），不跟着终端主题走：亮主题上默认那套
    会画成"浅底浅格"，暗主题上反过来——两种都有手机扫不出来的时候，而我们
    猜不到你那边是哪种。写死就没有这个变量。

    **同时存一张 PNG**（下面 QR_PNG_PATH）。终端里这张画得再清楚也没用：
    服务是 agent 以**后台任务**起的，输出落在任务日志里，用户根本看不到。
    所以真正管用的是那张图——agent 起来之后把它**内嵌进回复**（data URI），
    用户直接在对话里就看到、就能扫。

    存图那步是纯标准库，只要 `qrcode` 在就一定存得成。
    """
    qrcode = _ensure_qrcode()
    if qrcode is None:
        return
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make()
    white = "\x1b[47m\x1b[30m"   # 白底 + 黑格
    reset = "\x1b[0m"
    print("")
    print("  手机扫这个（%s）：" % url)
    if os.name != "nt":
        for row in qr.get_matrix():
            # 一格宽用两个字符：终端字符高宽比约 1:2，一格一字符会画成瘦长条
            line = "".join("██" if cell else "  " for cell in row)
            print(white + "  " + line + "  " + reset)

    try:
        with open(QR_PNG_PATH, "wb") as fh:
            fh.write(_qr_png_bytes(qr.get_matrix()))
        print("  二维码图片: %s（发给用户，对着屏幕扫）" % QR_PNG_PATH)
    except Exception as exc:
        print("  （二维码图片没存成：%s）" % exc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8971)
    ap.add_argument("--host", default=None,
                    help="绑到指定地址，逗号分隔可以写多个")
    ap.add_argument("--no-tailscale", action="store_true",
                    help="只绑回环：Tailscale 和局域网都不绑")
    ap.add_argument("--relay", nargs="?", const="claude", default=None,
                    metavar="APP",
                    help="中继模式：在当前会话里当一个入站中继（不占端口，"
                         "默认 claude）。要在**那个会话内部**起。")
    args = ap.parse_args()

    for ad in load_adapters():
        ADAPTERS[ad.name] = ad
        print("[bridge] 适配器已加载: %s (%s)" % (ad.name, ad.label))

    if not ADAPTERS:
        print("[bridge] 一个适配器都没加载成功，退出")
        return 1

    if args.relay:
        return run_relay(args.relay)

    # 挑地址放在中继后面：中继不绑端口，跑它之前去探 Tailscale（起子进程 + 超时 5 秒）
    # 只是白白挡在中继启动前面，而中继是越早起越好的那个。
    via = ""   # 远端地址是哪来的，横幅上要说清楚
    if args.host:
        hosts = [h.strip() for h in args.host.split(",") if h.strip()]
    elif args.no_tailscale:
        hosts = ["127.0.0.1"]
    else:
        # 默认**一定**绑一个远端地址：先问 Tailscale，没有就退到局域网。
        #
        # 早先是「默认只绑回环」，理由是暴露给谁该由使用者自己决定。但装这个
        # Skill 的人多半就是为了让手机连进来——只绑回环等于装完什么都干不了，
        # 还得自己去查 IP、改参数。查不到 Tailscale 就整个退回本机，更糟：
        # 现象是"手机打不开"，横幅上却只说一句"没找到 Tailscale IP"，
        # 看不出要装东西。远端地址是**承诺**，不能悄悄降级，所以退到局域网，
        # 并且把代价（同一网络里谁都能开）明写在横幅上。
        hosts = ["127.0.0.1"]
        remote, via = tailscale_ip(), "Tailscale"
        if not remote:
            remote, via = lan_ip(), "局域网"
        if remote and remote not in hosts:
            hosts.append(remote)

    servers = []
    for host in hosts:
        try:
            srv = ThreadingHTTPServer((host, args.port), Handler)
        except OSError as exc:
            print("[bridge] 绑 %s:%d 失败: %s" % (host, args.port, exc))
            continue
        servers.append((host, srv))
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    if not servers:
        print("[bridge] 一个地址都没绑上，退出")
        return 1

    print("")
    print("  phone-bridge 已启动")
    for host, _ in servers:
        print("    http://%s:%d" % (host, args.port))
    print("  适配器: %s" % ", ".join(sorted(ADAPTERS)))

    # 二维码只画远端那个地址：回环地址手机扫了打不开，画出来纯是误导。
    remote = [h for h, _ in servers if not h.startswith("127.") and h != "::1"]
    if remote:
        _print_qr("http://%s:%d" % (remote[0], args.port))
        if via and via != "Tailscale":
            # 退到局域网是**有代价**的，而且代价不小：这个桥没有鉴权。
            # 既然默认走了这条路，就得当面说清楚，不能让它悄悄发生。
            print("")
            print("  注意：上面这个是**局域网**地址（没查到 Tailscale IP）。")
            print("  同一网络里的任何人都能打开它，而这个桥没有密码。")
            print("  咖啡馆 / 酒店 / 公司网里别这么用——先装 Tailscale、登录同一")
            print("  账号再重启，就只在你自己的 tailnet 里可达了：")
            print("    https://tailscale.com/download")
    else:
        # 只有"自己选的"才不啰嗦：`--host` / `--no-tailscale` 是用户明确要只绑回环，
        # 再警告一遍就是废话。默认路径下走到这里，说明是真没辙了，必须说清楚。
        if not (args.host or args.no_tailscale):
            print("")
            print("  注意：没找到任何远端地址（Tailscale 没有，局域网也没有）——")
            print("  **手机现在连不上**，只有这台电脑能开 http://127.0.0.1:%d" % args.port)
            print("  想用手机连：装 Tailscale 并登录同一账号，然后重启本服务。")
            print("    https://tailscale.com/download")

    print("")
    print("  其他连法（想要 HTTPS 域名，或者不想退到局域网）：")
    print("    tailscale serve --bg %d     只在你的 tailnet 内可达，带 HTTPS" % args.port)
    print("    ssh -L %d:127.0.0.1:%d ...  从另一台机器转发过来" % (args.port, args.port))
    print("    ngrok / cloudflared ...      或者任何你惯用的隧道")
    print("    --host <ip>                  直接绑到某个网卡地址（自己注意网络边界）")
    print("    --no-tailscale               只绑回环，Tailscale 和局域网都不绑")
    print("")
    stop_event = threading.Event()
    threading.Thread(
        target=_watch_session, args=(stop_event,), daemon=True
    ).start()
    threading.Thread(
        target=_housekeeping, args=(stop_event,), daemon=True
    ).start()

    try:
        stop_event.wait()
    except KeyboardInterrupt:
        print("\n[bridge] 收到中断，退出")
    finally:
        for _, srv in servers:
            srv.shutdown()
            srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
