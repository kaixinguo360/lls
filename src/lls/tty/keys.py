"""按键字节流 → Key 事件解析。

覆盖模式（line/prompt）下使用。只识别我们需要的键：

* 可见字符（含多字节 UTF-8，交给增量解码器处理）
* 回车 / 退格 / Tab / Esc / 方向键
* Ctrl-* 组合（用于模式切换与取消）

不做完整的 readline 键绑定，那些属于 ``edit/keymap`` 的职责。
"""

from __future__ import annotations

import codecs

from .backend import Key

__all__ = ["KeyParser"]

# 控制字符 → 名称。字节 0x00-0x1f 的 ASCII 控制名。
_CONTROL_NAMES = {
    0x03: "ctrl_c",
    0x04: "ctrl_d",
    0x05: "ctrl_e",
    0x07: "ctrl_g",
    0x08: "ctrl_h",
    0x09: "tab",
    0x0a: "enter",      # \n
    0x0d: "enter",      # \r
    0x1b: "escape",
    0x7f: "backspace",
}

# 扩展转义序列（方向键等）→ 名称。键为含 ESC 前缀的完整字节序列。
_ESCAPE_SEQUENCES = {
    b"\x1b[A": "up",
    b"\x1b[B": "down",
    b"\x1b[C": "right",
    b"\x1b[D": "left",
    b"\x1bOA": "up",
    b"\x1bOB": "down",
    b"\x1bOC": "right",
    b"\x1bOD": "left",
    b"\x1b[H": "home",
    b"\x1b[F": "end",
    b"\x1b[3~": "delete",
}


class KeyParser:
    """增量解析按键字节流，产出 Key 事件。

    与屏幕解析器一样，``feed`` 接受任意切分的字节块（用户一次读入的字节）。
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._pending_escape: bytes = b""

    def feed(self, data: bytes) -> list[Key]:
        keys: list[Key] = []
        for byte in data:
            key = self._feed_byte(byte)
            if key is not None:
                keys.append(key)
        return keys

    def _feed_byte(self, byte: int) -> Key | None:
        raw = bytes([byte])

        # 正在累积 ESC 序列
        if self._pending_escape:
            self._pending_escape += raw
            seq = self._pending_escape
            name = _ESCAPE_SEQUENCES.get(seq)
            if name is not None:
                self._pending_escape = b""
                return Key(control=name, raw=seq)
            if len(seq) >= 3:
                # 未知序列：当作已结束的 ESC 处理，丢弃累积
                self._pending_escape = b""
                return Key(control="unknown", raw=seq)
            return None

        if byte == 0x1b:
            self._pending_escape = raw
            return None

        name = _CONTROL_NAMES.get(byte)
        if name is not None:
            return Key(control=name, raw=raw)

        # 可见字符：走增量解码
        ch = self._decoder.decode(raw)
        if ch:
            return Key(char=ch, raw=raw)
        return None

    def flush(self) -> Key | None:
        """强制产出残留（如未闭合的 ESC、未完成的多字节）。"""
        if self._pending_escape:
            seq = self._pending_escape
            self._pending_escape = b""
            return Key(control="unknown", raw=seq)
        tail = self._decoder.decode(b"", final=True)
        if tail:
            return Key(char=tail, raw=b"")
        return None
