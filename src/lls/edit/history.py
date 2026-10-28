"""edit 包：输入历史的持久化。

历史按 ``history_id`` 分文件，作用域只有两种：

* ``global``：命令行、行模式这类「本机上敲过的东西」，跨进程共用一份；
* ``session-<id>``：AI 会话专属的输入（任务描述、生成指令），换会话就换一份，
  会话删掉时整目录一起删。

文件是一行一条 JSON 的 JSONL。写入用 ``O_APPEND`` 一笔写完，多个 lls 同时跑也
不会把行写花；读取时解析不了的行直接丢 —— 进程被杀时留下半行是正常现象。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Callable

from lls.config import DEFAULT_HISTORY_MAX_BYTES, DEFAULT_HISTORY_MAX_ENTRIES, load

__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "HistoryStore",
    "default_history_dir",
    "forget_session",
    "history_from_config",
    "session_dir_name",
]

#: 载入进 ↑ 的条数上限：再多也翻不到，白占内存。
DEFAULT_MAX_ENTRIES = DEFAULT_HISTORY_MAX_ENTRIES

#: 单个历史文件超过它就顺手压缩（只在载入时做，不挡输入）。
DEFAULT_MAX_BYTES = DEFAULT_HISTORY_MAX_BYTES

#: 不跟着会话走的那一份：本机敲过的东西，跨进程共用。
_GLOBAL = "global"

#: 读文件的硬上限：历史文件被外部塞大也不差这一次全读。
_READ_LIMIT = 4 * 1024 * 1024

_BAD_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def default_history_dir() -> Path:
    """历史目录：配置里的 ``paths.history_dir``（默认 ``~/.local/lls/history``）。"""
    return load().paths.history_dir


def _safe(name: str) -> str:
    """把任意 id 变成能当文件名的样子：只换危险字符，保留可读性。"""
    cleaned = _BAD_CHARS.sub("_", name).lstrip(".")
    return cleaned or "unnamed"


def session_dir_name(session_id: str) -> str:
    """会话作用域在磁盘上的目录名。"""
    return "session-" + _safe(session_id)


def forget_session(session_id: str, root: Path | None = None) -> None:
    """删掉某条会话专属的历史目录：尽力而为，删不掉也不该影响删会话。"""
    if not session_id:
        return
    base = Path(root) if root is not None else default_history_dir()
    shutil.rmtree(str(base / session_dir_name(session_id)), ignore_errors=True)


def history_from_config(scope_of: Callable[[], str] | None = None) -> "HistoryStore | None":
    """按配置决定要不要落盘，并把容量参数交给 :class:`HistoryStore`。"""
    config = load()
    if not config.input_history.enabled:
        return None
    return HistoryStore(
        config.paths.history_dir,
        max_entries=config.input_history.max_entries,
        max_bytes=config.input_history.max_bytes,
        scope_of=scope_of,
    )


class HistoryStore:
    """把各 ``history_id`` 的输入历史落到磁盘，并在启动时读回来。"""

    def __init__(
        self,
        root: Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        scope_of: Callable[[], str] | None = None,
    ) -> None:
        self._root = Path(root)
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._scope_of = scope_of
        # 每个文件最后写进去的一条：去重时省掉一次读盘。
        self._last: dict[Path, str] = {}
        # 尾行不完整的文件：下次追加要先补一个换行，否则新记录会被粘进
        # 那半行里，一起变成解析不了的垃圾。
        self._unterminated: set[Path] = set()
        # 已经建好并收紧过权限的目录，省掉每次写入都 chmod。
        self._ready: set[Path] = set()
        # 第一次写失败的原因（家目录只读、磁盘满……）；历史存不下也得照常
        # 能干活，所以只记下来不打断输入，也不往 stderr 上打字。
        self.last_error: str | None = None

    # ---- 路径与作用域 ----

    def scope(self, history_id: str, *, session: bool = False) -> str:
        """某个 ``history_id`` 的历史属于谁：当前会话的目录，或 ``global``。

        ``session=True`` 表示这份历史跟当前 AI 会话走 —— 由调用方声明，
        而不是在这里列一张 id 清单：新加一处会话内的提示，只要记得传这个
        参数，就不会悄悄混进共用历史。
        """
        if not session:
            return _GLOBAL
        session_id = self._session_id()
        return session_dir_name(session_id) if session_id else _GLOBAL

    def path_for(self, history_id: str, *, session: bool = False) -> Path:
        scope = self.scope(history_id, session=session)
        return self._root / scope / (_safe(history_id) + ".jsonl")

    def _session_id(self) -> str:
        """当前 AI 会话 id。取不到就退化成 global，绝不因为查会话而丢历史。"""
        if self._scope_of is None:
            return ""
        try:
            return str(self._scope_of() or "")
        except Exception:  # noqa: BLE001 - 作用域解析失败不该打断输入
            return ""

    # ---- 读 ----

    def load(self, history_id: str, *, session: bool = False) -> list[str]:
        """读出可召回的历史（旧在前）；文件不在或读不了就是空列表。"""
        path = self.path_for(history_id, session=session)
        entries, terminated = _read_entries(path)
        if terminated:
            self._unterminated.discard(path)
        elif entries or _size(path):
            self._unterminated.add(path)
        values = entries[-self._max_entries :]
        if not values:
            return []
        self._last[path] = values[-1]
        if len(entries) > self._max_entries or _size(path) > self._max_bytes:
            # 只在冷路径上压缩：不挡输入，也不会越攒越大。
            self._compact(path, values)
        return values

    # ---- 写 ----

    def append(self, history_id: str, value: str, *, session: bool = False) -> None:
        """追加一条历史；空行与紧挨着的重复直接跳过。"""
        if not value.strip():
            return
        path = self.path_for(history_id, session=session)
        if path not in self._last:
            # 本进程第一次写这个文件：先看看磁盘上最后一条是什么，
            # 不然重启后接着敲同一条会写进两遍。
            self.load(history_id, session=session)
        if self._last.get(path) == value:
            return
        payload = _encode(value)
        if path in self._unterminated:
            payload = b"\n" + payload
        try:
            self._ensure_dir(path.parent)
            fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                _write_all(fd, payload)  # O_APPEND 下每笔都落在文件末尾
            finally:
                os.close(fd)
        except OSError as e:
            self._note_error(e, path)
            return
        self._last[path] = value
        self._unterminated.discard(path)

    def _ensure_dir(self, directory: Path) -> None:
        """目录建好，并且只让自己读 —— 输入里有命令，不该给同机其他人看。"""
        if directory in self._ready:
            return
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(str(directory), 0o700)
        except OSError:
            pass
        self._ready.add(directory)

    def _compact(self, path: Path, values: list[str]) -> None:
        """只留 ``values`` 重写文件：临时文件 + rename，读者看不到半成品。"""
        payload = b"".join(_encode(value) for value in values)
        # 行很长时按字节数触发的压缩其实什么都丢不掉，重写一遍只是白写，
        # 而且下次载入还会触发 —— 真能变小才动手。
        if len(payload) >= _size(path):
            return
        tmp = path.with_name(path.name + ".tmp")
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                _write_all(fd, payload)
            finally:
                os.close(fd)
            os.replace(str(tmp), str(path))
            self._unterminated.discard(path)
        except OSError as e:
            self._note_error(e, path)
            try:
                os.unlink(str(tmp))
            except OSError:
                pass

    def _note_error(self, error: OSError, path: Path) -> None:
        """只记第一次失败。

        项目里没有配置 logging handler，打 warning 只会落到 lastResort 直接
        写 stderr —— 交互时那几行会糊在覆盖层上。所以只留个字段，谁想提示
        谁去读。
        """
        if self.last_error is None:
            self.last_error = "%s: %s" % (path, error)


def _encode(value: str) -> bytes:
    record = {"v": 1, "text": value, "ts": round(time.time(), 3), "cwd": _cwd()}
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


def _cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return ""


def _write_all(fd: int, payload: bytes) -> None:
    """写满整笔：超大行时 ``write`` 可能只写进去一部分。"""
    view = memoryview(payload)
    while view:
        view = view[os.write(fd, view) :]


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_entries(path: Path) -> tuple[list[str], bool]:
    """读 JSONL，返回 (可用内容, 文件是否以换行结尾)。

    解析不了的行跳过 —— 进程被杀时留下半行是正常现象；「是否以换行结尾」
    交给调用方决定下次追加要不要先补一个换行。
    """
    try:
        size = _size(path)
        with open(str(path), "rb") as fh:
            if size > _READ_LIMIT:
                fh.seek(size - _READ_LIMIT)
                chunk = fh.read()
                # 首行多半被截断，丢掉；换行字节不会出现在多字节字符内部。
                chunk = chunk.split(b"\n", 1)[-1]
            else:
                chunk = fh.read()
    except OSError:
        return [], True
    values: list[str] = []
    for line in chunk.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        text = record.get("text") if isinstance(record, dict) else None
        if isinstance(text, str) and text:
            values.append(text)
    return values, chunk.endswith(b"\n")
