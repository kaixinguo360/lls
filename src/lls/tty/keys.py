"""按键字节流 → Key 事件解析。

命令模式下使用。字节流里混着三类东西，必须分开：

1. **用户按键**：可见字符、回车/退格/Tab、方向键、Ctrl-* 组合；
2. **终端 ↔ 程序的控制序列**：终端对查询的应答（DSR/DA/…）、程序设标题等。
   这些**不是用户输入**，必须整段转发给子进程，绝不能拆开当成按键；
3. **ESC 歧义**：``ESC`` 既可能是独立按键，也可能是序列前缀，靠超时区分。

本模块按 **ECMA-48 / VT100** 的转义序列语法实现（而不是枚举常见序列）：

* ``ESC`` + 中间字节(0x20-0x2F)* + 终结字节(0x30-0x7E) —— 两字节或带中间字节的转义
* ``ESC [`` + 参数字节(0x30-0x3F)* + 中间字节(0x20-0x2F)* + 终结字节(0x40-0x7E) —— CSI
* ``ESC O`` + 终结字节(0x40-0x7E) —— SS3（部分方向键）
* ``ESC ]`` / ``ESC P`` / ``ESC X`` / ``ESC ^`` / ``ESC _`` ... ``BEL`` 或 ``ESC \``
  —— 字符串型序列（OSC 设标题、DCS 等），内容可含任意可打印字符

任何按上述语法收全的序列，若不是已知按键，就整体交给子进程。
这样无论终端回什么格式的应答（``ESC[?12;1$y``、``ESC[>41;354;0c`` …
还是 ``ESC]11;rgb:...``），都不会有字符漏成可见输入。
"""

from __future__ import annotations

import codecs

from .backend import Key

__all__ = ["KeyParser"]

# C0 控制字符 → 名称。
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

# 已知按键序列。其余符合语法的序列一律视为控制序列转发给子进程。
_KEY_SEQUENCES = {
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
    b"\x1b[1~": "home",
    b"\x1b[4~": "end",
    b"\x1b[3~": "delete",
}

# 解析状态
_GROUND = 0     # 普通字节
_ESC = 1        # 已收到 ESC
_CSI = 2        # 已收到 ESC [
_SS3 = 3        # 已收到 ESC O
_STRING = 4     # 已收到 ESC ] / P / X / ^ / _（等到 BEL 或 ESC \）

_STRING_INTRODUCERS = frozenset(b"]PX^_")


def _is_final(byte: int) -> bool:
    """终结字节：0x30-0x7E（ESC 序列）/ 0x40-0x7E（CSI、SS3）。"""
    return 0x40 <= byte <= 0x7E


class KeyParser:
    """增量解析按键字节流，产出 Key 事件。

    ``feed`` 接受任意切分的字节块（对应一次 read）。语法未收全时内部挂起，
    等后续字节；调用方在看到 :attr:`pending_escape` 非空且暂无更多字节时，
    等一个短超时后调用 :meth:`flush_escape` 把挂起内容判为独立 Esc 键。
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._state = _GROUND
        self._buf = b""

    @property
    def pending_escape(self) -> bytes:
        """尚未收全的控制序列（空表示没有挂起）。"""
        return self._buf if self._state != _GROUND else b""

    # ---- 输入 ----

    def feed(self, data: bytes) -> list[Key]:
        keys: list[Key] = []
        for byte in data:
            keys.extend(self._feed_byte(byte))
        return keys

    def _feed_byte(self, byte: int) -> list[Key]:
        if self._state == _GROUND:
            return self._feed_ground(byte)

        self._buf += bytes([byte])
        return self._advance(byte)

    def _feed_ground(self, byte: int) -> list[Key]:
        if byte == 0x1B:
            self._state = _ESC
            self._buf = b"\x1b"
            return []
        name = _CONTROL_NAMES.get(byte)
        if name is not None:
            return [Key(control=name, raw=bytes([byte]))]
        ch = self._decoder.decode(bytes([byte]))
        return [Key(char=ch, raw=bytes([byte]))] if ch else []

    def _advance(self, byte: int) -> list[Key]:
        """按语法推进当前序列；收全则产出事件。"""
        if self._state == _ESC:
            return self._advance_esc(byte)
        if self._state == _CSI:
            # ESC [ + 参数/中间字节* + 终结字节
            if _is_final(byte):
                return self._finish()
            if 0x20 <= byte <= 0x3F:
                return []
            return self._finish_unknown()
        if self._state == _SS3:
            # ESC O + 一个终结字节
            if _is_final(byte):
                return self._finish()
            return self._finish_unknown()
        if self._state == _STRING:
            # 字符串型：BEL 或 ESC \ 收尾，内容里什么字节都可能有
            if byte == 0x07 or self._buf.endswith(b"\x1b\\"):
                return self._finish()
            return []
        return self._finish_unknown()

    def _advance_esc(self, byte: int) -> list[Key]:
        """ESC 之后的第二个字节决定序列种类。"""
        if byte == 0x5B:                 # '['
            self._state = _CSI
            return []
        if byte == 0x4F:                 # 'O'
            self._state = _SS3
            return []
        if byte in _STRING_INTRODUCERS:  # ] P X ^ _
            self._state = _STRING
            return []
        if 0x20 <= byte <= 0x2F:         # 中间字节，继续等终结字节
            return []
        if 0x30 <= byte <= 0x7E:         # ESC + 终结字节 = 两字节转义
            return self._finish_esc_pair(byte)
        return self._finish_unknown()

    # ---- 收尾 ----

    def _finish_esc_pair(self, byte: int) -> list[Key]:
        """两字节转义（``ESC x``）。

        歧义处理：用户按 Esc 再敲字符，字节流同样是 ``ESC x``。
        与 readline 一致 —— 不是已知按键就按「Esc 键 + 该字符」处理，
        否则用户按 Esc 之后打的字会被当成控制序列吞掉。
        """
        self._state = _GROUND
        self._buf = b""
        return [
            Key(control="escape", raw=b"\x1b"),
            *self._feed_ground(byte),
        ]

    def _finish(self) -> list[Key]:
        """序列收全：已知按键产出对应 key，其余作为控制序列交给子进程。"""
        seq = self._buf
        self._state = _GROUND
        self._buf = b""
        name = _KEY_SEQUENCES.get(seq)
        if name is not None:
            return [Key(control=name, raw=seq)]
        return [Key(control="terminal_response", raw=seq)]

    def _finish_unknown(self) -> list[Key]:
        """不符合语法的残片：整体丢弃，绝不让残余字节漏成可见输入。"""
        seq = self._buf
        self._state = _GROUND
        self._buf = b""
        return [Key(control="unknown", raw=seq)]

    # ---- 超时 ----

    def flush_escape(self) -> Key | None:
        """把挂起内容判为终结。

        单独的 ``ESC`` → ``escape`` 键（用户按了 Esc）；
        其余半截序列 → ``unknown`` 丢弃（后续字节不会再来）。
        """
        if self._state == _GROUND:
            return None
        seq = self._buf
        self._state = _GROUND
        self._buf = b""
        if seq == b"\x1b":
            return Key(control="escape", raw=seq)
        return Key(control="unknown", raw=seq)

    def flush(self) -> Key | None:
        """强制产出残留（解码器里未完成的多字节字符）。"""
        key = self.flush_escape()
        if key is not None:
            return key
        tail = self._decoder.decode(b"", final=True)
        if tail:
            return Key(char=tail, raw=b"")
        return None
