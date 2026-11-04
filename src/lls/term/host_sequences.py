"""从子进程字节流里挑出「宿主状态」序列。

pyte 只认 OSC 0/1/2 的标题，其余一概丢掉。有三类序列虽不影响画面，却要告诉
宿主（或由宿主决定）才行：

* ``CSI Ps SP q`` —— 光标形状（DECSCUSR）；
* ``OSC 0 ; 文本`` / ``OSC 2 ; 文本`` —— 窗口标题；
* ``OSC 52 ; 选区 ; base64`` —— 剪贴板。

本模块只做这一件事：按字节扫描、容忍任意切分、对超长的 OSC 有界。它不是
通用转义解析器 —— 除了上面三类，别的一律跳过，交给 pyte。
"""

from __future__ import annotations

__all__ = ["HostSequences", "MAX_PENDING"]

#: 半条序列最多攒这么多字节：OSC 52 的载荷可能有几 KB，再长就丢弃。
#: 没有这个上限，一段永远不闭合的 OSC 会让缓冲无限增长。
MAX_PENDING = 8192


class HostSequences:
    """扫描器：吃字节，留下最近一次的光标形状 / 标题 / 剪贴板。

    三个字段都是"最近一次生效的值"，不是事件队列：引擎按值判断要不要转发，
    重复设置同一个值不会产生任何下发。
    """

    def __init__(self) -> None:
        self.cursor_style: int | None = None
        self.title = ""
        self.clipboard: str | None = None
        self._pending = b""

    def reset(self) -> None:
        """回到干净状态（换一块屏幕模型时用）。"""
        self.cursor_style = None
        self.title = ""
        self.clipboard = None
        self._pending = b""

    def feed(self, data: bytes) -> None:
        buf = self._pending + data
        self._pending = b""
        index = 0
        total = len(buf)
        while index < total:
            start = buf.find(b"\x1b", index)
            if start < 0:
                return
            if start == total - 1:
                # 孤零零一个 ESC：下一块的第一字节才决定它是什么序列。
                self._keep_until(buf, start)
                return
            if buf.startswith(b"\x1b]", start):
                end = self._osc_end(buf, start + 2)
                if end is None:
                    self._keep_until(buf, start)
                    return
                self._osc(buf[start + 2 : end])
                index = end + (1 if buf[end : end + 1] == b"\x07" else 2)
                continue
            if buf.startswith(b"\x1b[", start):
                end = self._csi_end(buf, start)
                if end is None:
                    self._keep_until(buf, start)
                    return
                self._csi(buf[start + 2 : end])
                index = end
                continue
            # 别的转义序列（ESC M、ESC ( B 等）：不管，跳过 ESC 继续找。
            index = start + 1

    def _keep_until(self, buf: bytes, start: int) -> None:
        """把半条序列留到下一块；超长就丢掉（不再等它闭合）。"""
        tail = buf[start:]
        self._pending = tail if len(tail) <= MAX_PENDING else b""

    @staticmethod
    def _osc_end(buf: bytes, start: int) -> int | None:
        """OSC 的终止符位置（BEL 或 ESC \\），没有则 ``None``。"""
        bel = buf.find(b"\x07", start)
        st = buf.find(b"\x1b\\", start)
        found = [pos for pos in (bel, st) if pos >= 0]
        return min(found) if found else None

    @staticmethod
    def _csi_end(buf: bytes, start: int) -> int | None:
        """CSI 的终字节（0x40–0x7E）之后的位置，没有则 ``None``。"""
        index = start + 2
        total = len(buf)
        while index < total:
            byte = buf[index]
            if 0x40 <= byte <= 0x7E:
                return index + 1
            if byte < 0x20:
                return index  # 控制字符：这不是一条 CSI，就此打住
            index += 1
        return None

    def _csi(self, body: bytes) -> None:
        """``CSI Ps SP q`` 是光标形状；其余 CSI 与我们无关。"""
        if not body.endswith(b" q"):
            return
        raw = body[:-2].strip()
        self.cursor_style = int(raw) if raw.isdigit() else 0

    def _osc(self, body: bytes) -> None:
        head, _, rest = body.partition(b";")
        if head in (b"0", b"2"):
            self.title = _plain(rest)
            return
        if head != b"52":
            return
        _, _, payload = rest.partition(b";")
        if payload:
            self.clipboard = payload.decode("ascii", "replace")


def _plain(raw: bytes) -> str:
    """标题里不该出现控制字符：它们是终端指令，不是文字。"""
    text = raw.decode("utf-8", "replace")
    return "".join(char for char in text if char.isprintable())
