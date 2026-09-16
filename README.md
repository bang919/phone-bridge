# phone-bridge

把桌面上正在跑的 AI 会话（Claude Desktop、Codex）接到手机浏览器上——**不装任何 App**，手机端只是一个网页。

出门在外想接着跟电脑上那个对话聊，打开手机就能继续。翻的是**全部**对话，不只是某一个。

> macOS 专用。依赖 Claude 桌面版自己的进程与 socket 布局，其他平台跑不起来。

## 它长什么样

```
手机 Safari
  │  隧道（tailscale serve / ssh -L / 随便你惯用什么）
  ▼
bridge.py  ← HTTP 服务，默认绑 127.0.0.1 + 自动检测到的 Tailscale IP
  ├─ 出站：各 App 适配器读自己的会话日志
  └─ 入站：各 App 适配器写回自己的通道
```

**一个前端管所有会话，一个端口（默认 8971）就够。** 不需要每个对话跑一个 bridge，也不需要多个端口。

| 手机上的状态 | 含义 |
|---|---|
| 绿点 | 在跑且闲着 |
| 橙点闪 | 在跑且正在干活（想事情 / 跑工具 / 压缩上下文） |
| 没点 | 进程已经没了——历史还在，另起一个 agent 接着聊 |
| 「正在干活…」 | 忙也能发，消息会被排队，不是发不进去 |

## 安装

```bash
git clone https://github.com/bang919/phone-bridge.git ~/.claude/skills/phone-bridge
```

装到 `~/.claude/skills/` 是**用户级**，所有项目都能用。只想在某个项目里用就装到那个项目的 `.claude/skills/phone-bridge/`。

## 启动

**这一步错了整个入站就是废的，而且不会报错。** 必须让 bridge 一直是会话的**子进程**——
`nohup ... &` 会让它被 reparent 到 launchd，于是它照样能连上 socket、照样返回 `{"ok": true}`，
**而帧会被静默丢弃**。

所以在**某个 Claude Code 会话里**起（Claude Code 里就是 Bash 工具的 `run_in_background`）：

```bash
python3 -u ~/.claude/skills/phone-bridge/bridge.py --port 8971
```

起完自查一遍，确认 claude 进程在 bridge 的祖先链里：

```bash
pid=$(pgrep -f "phone-bridge/bridge.py" | head -1)
while [ "$pid" != "1" ] && [ -n "$pid" ]; do
  ps -o pid=,ppid=,command= -p "$pid" | cut -c1-100
  pid=$(ps -o ppid= -p "$pid" | tr -d ' ')
done
```

链里出现 `claude --resume=...` 才算对。出现 `(launchd)` 直接就是错的。

启动横幅会打印手机该开的地址（Tailscale IP）。

## 三条写入路径

入站靠**进程树**认证，而一个进程只有一个父进程——所以「资格」没法共享，只能按「离那个对话有多近」分三条路：

| 那个对话现在什么状态 | 怎么发 |
|---|---|
| 前端出生的那**一个**会话 | 直接注入（前端就在它的树里） |
| **别的正开着**的会话 | 交给**住在它进程树里的中继**代发 |
| **根本没在跑**的历史对话 | 前端自己**现场起一个 headless 会话**，把话写进它的 stdin |

### 让别的正开着的会话也能写：中继

在**那个会话内部**以后台任务方式起一行：

```bash
python3 -u ~/.claude/skills/phone-bridge/bridge.py --relay
```

中继**不是第二个端口、更不是第二个地址**——它只在 `/tmp/phone-bridge-relays/<sessionId>.<pid>.sock` 上听一个 UDS 文件，前端自动发现它。手机那边永远只有一个 URL、不用改任何配置。

一个中继只对一个会话有效。**出门前**记得在你打算用的那个会话里先起好，否则它只能看不能发（界面上会明说原因，不会静默失败）。

### 没在跑的历史对话：自起

不用配置，前端会现场拉起一个 headless 会话。**但它不是「接管」，是「另起一个脑子」：**

- 它是个**真 agent**，会回话、会动工具。
- 它和桌面窗口**抢同一份 transcript**。所以只在「这个对话没在跑」时才走这条路。
- **别同时又在电脑上打开那个对话。** 那会变成两个脑袋。
- 权限提示没人接，需要授权的工具那一轮会被拒。
- 界面上会明说这件事，不假装是你在跟原来那个说话。

## 最重要的一条：手机发的话不算「用户意图」

UDS 注入的消息以 **peer（跨会话）身份**到达，Claude Code 会给它套上免责声明，界面上标成「来自外部注入」：

> Another Claude session sent a message: … This came from another Claude session
> — **not typed by your user**, but very likely working on their behalf.

这是**系统提示里的硬性条款**，不是可绕过的实现细节：

- 日常继续对话、让它干活 —— 没问题。
- **授权类的不算数。** 你在手机上回「就这个，动手吧」，不会被当成你的批准，会被当作自主行为评估。

所以：把它当「出门在外能接着说」的通道，**不是**「出门在外能替我按确认」的通道。

## 安全

**这个桥没有鉴权。** 能碰到这个端口的人就能往你的 AI 会话里发消息。

- 默认只绑 `127.0.0.1` + Tailscale IP（同一 tailnet 内可达）。
- **别绑 `0.0.0.0`**，那会连咖啡馆的 wifi 一起暴露。用 `--no-tailscale` 可以只绑回环。
- 也别加 `Access-Control-Allow-Origin: *`，那等于让任意网页都能发。

## 已知限制

- **claude 入站靠进程树关系认证**，必须是会话的子孙进程。`can_send()` 会真的去查祖先链，查不到就返回 `False` 并说明原因——**不会假装能发**。
- **Codex 只能读不能发。** 入站通道（`~/.codex/ipc/ipc.sock` 的握手）还没逆出来。
- **bridge 随会话生死。** 会话结束它跟着走，会话重启后要重新起。`pkill -f "phone-bridge/bridge.py"` 可以随时手动关掉前端和中继。
- UDS 是 Claude Code 的**内部协议，非公开 API**，版本升级可能失效。失效时表现为「连得上但没反应」，先按祖先链自查。
- **桌面窗口只在打开那一刻读一次记录，之后不回读。** 所以一个已经开着的窗口看不到之后任何人写的话——CLI 写的、自起进程写的、手机写的，一视同仁。把对话关掉重开就都在了。

## 关掉

**自己会关的情况只有一个：退出 Claude 桌面 App。** 其他都不关（关窗口不会、很久不聊也不会）。

```bash
pkill -f "phone-bridge/bridge.py"          # 关
pgrep -fl "phone-bridge/bridge.py" || echo "已经关了"   # 确认
```

关掉 bridge 不影响会话本身——没的只是那个网页服务。

## 文档

| 文件 | 内容 |
|---|---|
| [SKILL.md](SKILL.md) | 给 Claude Code 用的完整说明：架构、陷阱、UI 约定、排错 |
| [PORTING.md](PORTING.md) | **接入一个新的 App 之前先读这个。** 从零逆出一套通道的通用方法：怎么找入站、怎么验认证、怎么 grep 二进制逆协议、怎么剥壳、怎么验证 |

## 新增一个 App

在 `adapters/` 下加一个文件，继承 `base.Adapter`，实现四个方法（`sessions` / `messages` / `can_send` / `send`），然后在 `adapters/__init__.py` 的 `load_adapters()` 里注册。**改完必须重启 bridge。**

读 [PORTING.md](PORTING.md) 再动手——那里记着踩过的坑，能省你一天。
