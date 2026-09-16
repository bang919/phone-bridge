"""适配器接口。

出站 = messages()：把某个会话读成一串 {role, text, ts}
入站 = send()：把一条消息写回那个会话
"""


class Adapter(object):
    name = ""      # 稳定 id，前端用它来区分
    label = ""     # 显示名

    def sessions(self, limit=80):
        """返回会话列表，每项：
        {app, id, title, cwd, mtime, live, busy, can_send}
        """
        return []

    def messages(self, sid, limit=400):
        """返回消息列表，每项：{role, text, ts}"""
        return []

    def is_busy(self, sid):
        """这个会话此刻是不是正在干活（想事情 / 跑工具 / 压缩上下文）。

        纯提示用：手机上看到「忙」就知道别催，等它自己说完。
        拿不到就返回 None —— 「不知道」和「闲着」是两回事，别混。
        """
        return None

    def can_send(self, sid):
        return False

    def blocked_reason(self, sid):
        """不能发时，用一句人话说明为什么。能发则返回 None。

        有它才不会出现「界面显示可发送、消息实际石沉大海」这种情况。
        """
        if self.can_send(sid):
            return None
        return "这个会话不支持发送"

    def send(self, sid, text):
        raise NotImplementedError("这个 App 暂不支持入站")


def text_from_content(content):
    """把 Anthropic / OpenAI 两种 content 结构都拍平成纯文本。"""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype in ("text", "input_text", "output_text"):
                    t = block.get("text")
                    if isinstance(t, str):
                        parts.append(t)
    return "\n".join(parts)
