#!/usr/bin/env python3
"""phone-bridge —— 把本机跑着的 AI 会话接到手机浏览器上。

出站：各 App 适配器读自己的会话日志
入站：各 App 适配器写回自己的通道

启动（务必从会话内部启动，入站依赖进程树关系）：
    python3 bridge.py --port 8971
"""

import argparse
import json
import os
import socket
import subprocess
import sys
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
    for cand in (
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "tailscale",
    ):
        try:
            out = subprocess.check_output(
                [cand, "ip", "-4"],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            ip = out.decode().strip().splitlines()[0].strip()
            if ip:
                return ip
        except Exception:
            continue
    return "127.0.0.1"


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
        out = []
        for name, ad in ADAPTERS.items():
            try:
                for s in ad.sessions():
                    if not s.get("can_send"):
                        try:
                            s["blocked"] = ad.blocked_reason(s.get("id"))
                        except Exception:
                            s["blocked"] = None
                    out.append(s)
            except Exception as exc:
                print("[bridge] %s.sessions() 出错: %s" % (name, exc))
        out.sort(key=lambda s: s.get("mtime") or 0, reverse=True)
        return out


def _watch_session(stop_event):
    """会话没了，自己走。

    子进程不会随父进程一起死：父进程一没，它会被 launchd 收养，然后一直
    跑下去（这个坑踩过——一个被 nohup 起的 bridge 在启动者消失后还活着）。
    所以得自己盯：发现被收养了就说明会话结束了，自己了断。
    """
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8971)
    ap.add_argument("--host", default=None,
                    help="绑到指定地址，逗号分隔可以写多个")
    ap.add_argument("--no-tailscale", action="store_true",
                    help="不自动绑 Tailscale IP，只绑回环")
    ap.add_argument("--relay", nargs="?", const="claude", default=None,
                    metavar="APP",
                    help="中继模式：在当前会话里当一个入站中继（不占端口，"
                         "默认 claude）。要在**那个会话内部**起。")
    args = ap.parse_args()

    if args.host:
        hosts = [h.strip() for h in args.host.split(",") if h.strip()]
    elif args.no_tailscale:
        hosts = ["127.0.0.1"]
    else:
        # 默认连 Tailscale IP 一起绑。
        #
        # 早先是「默认只绑回环」，理由是暴露给谁该由使用者自己决定。但装这个
        # Skill 的人多半就是为了让手机连进来，每次都得记得加参数是个错的默认
        # ——别人装完直接用，才是对的。查不到就退回只绑回环，功能不受影响。
        hosts = ["127.0.0.1"]
        ts = tailscale_ip()
        if ts and ts != "127.0.0.1":
            hosts.append(ts)
        else:
            print("[bridge] 没找到 Tailscale IP，只绑了本机"
                  "（装了还这样，看它登录没有）")

    for ad in load_adapters():
        ADAPTERS[ad.name] = ad
        print("[bridge] 适配器已加载: %s (%s)" % (ad.name, ad.label))

    if not ADAPTERS:
        print("[bridge] 一个适配器都没加载成功，退出")
        return 1

    if args.relay:
        return run_relay(args.relay)

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
    print("")
    print("  手机要连进来：上面绑了 Tailscale IP 就用那个，否则看下面几种：")
    print("    tailscale serve --bg %d     只在你的 tailnet 内可达，带 HTTPS" % args.port)
    print("    ssh -L %d:127.0.0.1:%d ...  从另一台机器转发过来" % (args.port, args.port))
    print("    ngrok / cloudflared ...      或者任何你惯用的隧道")
    print("    --host <ip>                  直接绑到某个网卡地址（自己注意网络边界）")
    print("    --no-tailscale               只绑回环，不绑 Tailscale IP")
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
