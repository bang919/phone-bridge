# phone-bridge

在手机浏览器里接着聊电脑上的 AI 对话。**不装 App**，就一个网页。

## 怎么用

1. **电脑上**，在 Claude Code 里说一句「起一下 phone-bridge」。它会加载这个 skill、把服务跑起来，并打印一个地址。
2. **手机上**装 [Tailscale](https://tailscale.com/download)，登录**同一个账号**，然后打开那个地址。

就这样。手机上能看到全部对话，也能发消息。

## 现在能做什么

| | 看 | 发 |
|---|---|---|
| Claude | ✅ | ✅ |
| Codex | ✅ | ❌ 还没做 |

## 注意

- **只在 macOS 上能用。** 依赖 Claude 桌面版自己的进程布局。
- 手机上发的话**不算你的「批准」**。日常接着聊没问题；涉及危险操作、边界、权限的，别指望手机上这一句能通过。
- 服务**没有密码**，所以只走 Tailscale 内网，**别往公网暴露**。

想让**别的**正开着的对话也能发，在**那个对话里**跑一行（出门前先跑好）：

```bash
python3 -u ~/.claude/skills/phone-bridge/bridge.py --relay
```

## 安装

```bash
git clone https://github.com/bang919/phone-bridge.git ~/.claude/skills/phone-bridge
```

## 更多

- [SKILL.md](SKILL.md) —— 给 Claude Code 用的完整说明：架构、陷阱、UI 约定、排错
- [PORTING.md](PORTING.md) —— **接一个新 App 之前先读**：从零逆一套通道的通用方法
