# phone-bridge

在手机浏览器里接着聊电脑上的 AI 对话。**不装 App**，就一个网页。

## 怎么用

1. **电脑上**启动 `bridge.py`。在 Claude Code 里说一句「启动 phone-bridge」也可以，或者直接运行：

   ```bash
   python bridge.py --port 8971
   ```

   服务会把地址打印出来；缺 `qrcode` 会自动安装，并生成一张二维码图片。

   > **Windows 必须从普通 PowerShell / 终端启动。** 如果从受限工具沙箱启动，
   > `bridge.py` 调用 `codex queue` 时会继承写入限制，手机发送报
   > `state_5.sqlite ... readonly database`。这不是 Codex 会话坏了，换到普通
   > 终端重启 bridge 即可。

2. **手机上**装 [Tailscale](https://tailscale.com/download)，登录**同一个账号**，然后扫那张码（或打开那个地址）。

就这样。手机上能看到全部对话，也能发消息。

> 没装 Tailscale 也照样能用：服务会退到**局域网**地址，同一个 wifi 下就能连。
> 但那时候**同一个网络里的任何人都能打开它**（服务没有密码），所以在咖啡馆、酒店、
> 公司网里别这么用——先把 Tailscale 装好。

## 现在能做什么

| | 看 | 发 |
|---|---|---|
| Claude | ✅ | ✅ |
| Codex | ✅ | ✅ |

Codex 只有**桌面版开的**会话能发（终端里跑的接不进去）。发出去是让 Codex
真的跑一轮，跟在电脑上打字一样——**电脑上那个窗口没开也会跑**，回头打开就能看到。

## 注意

- **macOS 和 Windows 都支持。**
  - macOS 的 Claude 入站走 UDS，并受会话进程树认证；别的会话需要启动中继。
  - Windows 的 Claude 入站走命名管道，用会话自己的 token 认证；拿得到 token 的 bridge
    可以直接写入所有支持 peer messaging 的活动会话，**不需要中继**。
  - Codex 在两个平台都走桌面版自带的 `codex queue`。
- 手机上发的话**不算你的「批准」**。日常接着聊没问题；涉及危险操作、边界、权限的，别指望手机上这一句能通过。
- 服务**没有密码**，所以优先走 Tailscale 内网。没装 Tailscale 时会退到局域网地址
  （同一个 wifi 下谁都能开）——**别往公网暴露**。

macOS 上，Claude 只有**正在跑、并且起了中继**的对话能发；点灰掉的会话会提示在
对应对话里启动 phone-bridge。Windows 上 Claude 有活动进程、管道和 token 就能发，
不需要逐个启动中继。没在跑的历史对话在两个平台都只能看。

## 安装

```bash
git clone https://github.com/bang919/phone-bridge.git ~/.claude/skills/phone-bridge
```

只依赖系统自带的 python3。二维码要一个 `qrcode` 包，**没装的话服务启动时会自己装**
（`pip install --user`，装之前会先说一声）；万一装不上，也会明确告诉你少的是什么，
不会默默少一张图。想提前装好：

```bash
pip install qrcode
```

## 更多

- [SKILL.md](SKILL.md) —— 给 Claude Code 用的完整说明：架构、陷阱、UI 约定、排错
- [PORTING.md](PORTING.md) —— **接一个新 App 之前先读**：从零逆一套通道的通用方法
