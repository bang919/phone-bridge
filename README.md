# phone-bridge

在手机浏览器里接着聊电脑上的 AI 对话。**不装 App**，就一个网页。

## 怎么用

1. **电脑上**，在 Claude Code 里说一句「启动 phone-bridge」。它会加载这个 skill、把服务跑起来，并打印一个地址。
2. **手机上**装 [Tailscale](https://tailscale.com/download)，登录**同一个账号**，然后打开那个地址。

就这样。手机上能看到全部对话，也能发消息。

## 现在能做什么

| | 看 | 发 |
|---|---|---|
| Claude | ✅ | ✅ |
| Codex | ✅ | ✅ |

Codex 只有**桌面版开的**会话能发（终端里跑的接不进去）。发出去是让 Codex
真的跑一轮，跟在电脑上打字一样——**电脑上那个窗口没开也会跑**，回头打开就能看到。

## 注意

- **只在 macOS 上能用。** 依赖 Claude 桌面版自己的进程布局。
- 手机上发的话**不算你的「批准」**。日常接着聊没问题；涉及危险操作、边界、权限的，别指望手机上这一句能通过。
- 服务**没有密码**，所以只走 Tailscale 内网，**别往公网暴露**。

手机上只有**正在跑、并且起了服务**的对话能发——列表里它们在「已启动
phone-bridge」那一组。其余的整行变灰，点进去会告诉你怎么办：**在电脑上打开那个
对话，跟它说一句「启动 phone-bridge」**。出门前先把它启动好就行。

## 安装

```bash
git clone https://github.com/bang919/phone-bridge.git ~/.claude/skills/phone-bridge
```

## 更多

- [SKILL.md](SKILL.md) —— 给 Claude Code 用的完整说明：架构、陷阱、UI 约定、排错
- [PORTING.md](PORTING.md) —— **接一个新 App 之前先读**：从零逆一套通道的通用方法
