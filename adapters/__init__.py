"""phone-bridge 适配器注册表。

每个 App 一个适配器：各自负责自己的「入站」（把手机的消息写回去）
和「出站」（读取该 App 的会话内容）。桥本身不关心 App 细节。
"""

from .base import Adapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter


def load_adapters():
    out = []
    for cls in (ClaudeAdapter, CodexAdapter):
        try:
            out.append(cls())
        except Exception as exc:  # 某个 App 的日志目录不存在时跳过，不影响其他
            print("[bridge] 适配器 %s 加载失败: %s" % (cls.__name__, exc))
    return out
