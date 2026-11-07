"""Run a handler over a terminal.

chat keeps the existing headless agent path. --handler runs an external handler;
with a TTY and without --headless it uses the normal HostUI. A controllable
handler gets the Ctrl-E control plane automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import os
import sys
from dataclasses import dataclass
from typing import Any

from .ai import ConfigError, default_catalog
from .ai.prompt import HELP as TEACHER_HELP
from .ai.prompt import build as build_prompt
from .ai.sessions import SessionError, default_budget, store
from .ai.tools import ScreenView, Toolbox, builtin_tools, teacher_tools
from .ai.tools.policy import ToolPolicy, global_policy, reset_global_policy
from .ai.turn import Turn, TurnState
from .config import load as load_config
from .handlers import ControlHandler
from .session.engine import Engine
from .session.errors import SessionEnded
from .term import PyteScreenModel
from .tty import make_backend

__all__ = ["main"]

ROWS = 24
COLUMNS = 80
PAINT_INTERVAL = 0.2


@dataclass(frozen=True)
class _Options:
    task: str
    session: str
    headless: bool
    stay: bool
    command: list[str]
    handler: str = "chat"


def _parse(argv: list[str]) -> _Options:
    parser = argparse.ArgumentParser(
        prog="lls serve",
        description="无头运行：把终端交给 agent，反复执行直到退出。",
    )
    parser.add_argument("--task", default="", help="这一轮的任务；省略则不追加用户消息")
    parser.add_argument("--session", default="", help="接着已有会话（id/前缀/标题片段）")
    parser.add_argument("--new", action="store_true", help="新建会话（默认行为）")
    parser.add_argument("--headless", action="store_true", help="不画屏，只把最终答复写到 stdout")
    parser.add_argument("--stay", action="store_true", help="模型停下之后立刻再来一轮，直到被中断")
    parser.add_argument("--handler", default="chat",
                        help="chat（默认）或 module:Class；后者直接作为 Engine handler")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="被包装的命令；默认 $SHELL")
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    return _Options(
        task=(args.task or "").strip(),
        session=(args.session or "").strip(),
        headless=bool(args.headless),
        stay=bool(args.stay),
        command=command or [_default_shell()],
        handler=(args.handler or "chat").strip(),
    )


def _default_shell() -> str:
    shell = os.environ.get("SHELL")
    return shell or os.environ.get("LLS_FALLBACK_SHELL", "/bin/sh")


class _Serve:
    """引擎的处理器：进去跑 agent，跑完叫停引擎。

    只跑一次（``step`` 可能被主循环反复调用，第二次是空转）—— 无头运行没有
    「回到提示符」这回事，干完就该退出。
    """

    passthrough = False

    def __init__(
        self,
        options: _Options,
        session: Any,
        provider: Any,
        budget: int,
        catalog: Any,
    ) -> None:
        self._options = options
        self._session = session
        self._provider = provider
        self._budget = budget
        self._catalog = catalog
        self._started = False
        self.status = "error"
        self.text = ""

    async def step(self, services: Any) -> None:
        if self._started:
            return
        self._started = True
        try:
            await self._drive(services)
        except asyncio.CancelledError:
            raise
        except SessionEnded:
            # 子进程没了，或者有人按了中断：这是无头运行的正常收尾方式。
            self.status = "closed"
        except SessionError as e:
            self.status = "closed"
            print(f"lls serve: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - 无头运行没有别的出口
            self.status = "error"
            print(f"lls serve: {e}", file=sys.stderr)
        finally:
            services.stop()

    async def _drive(self, services: Any) -> None:
        view = ScreenView(services, services.wait)
        all_tools = builtin_tools(services, services.wait, view=view)
        state = TurnState()
        teacher = self._teacher_tools(state)
        all_tools.extend(teacher)
        policy = global_policy().merged(
            ToolPolicy.from_json(self._session.meta.tools)
        )
        tools = Toolbox(policy.filter(list(all_tools)))
        has_teacher = any(entry.tool.name == "ask_teacher" for entry in tools)
        system = build_prompt(
            tools,
            # serve 没有会话提示这一层：答复要么写到 stdout，要么由
            # ``_run`` 在收尾时打印，所以一律按「没有人看」来写。
            display="headless",
            mode="task" if self._options.task else "standby",
        ) + (TEACHER_HELP if has_teacher else "")

        turn_no = 0
        task: Any = self._options.task or None
        while True:
            turn_no += 1
            turn = Turn(
                services=services,
                view=view,
                provider=self._provider,
                model=self._session.meta.model_id,
                session=self._session,
                tools=tools,
                budget=self._budget,
                provider_id=self._session.meta.provider_id,
                catalog=self._catalog,
                system=system,
                state=state,
                turn=turn_no,
                poll=services.wait,
            )
            result = await turn.run(task)
            self.status = result.status
            self.text = result.text
            # 有任务的那一轮，答复就是结果；待命期间每轮都可能有话，
            # 全写进会话，别往 stdout 刷屏。
            if (
                result.status == "ok"
                and not self._options.stay
                and result.text
                and self._options.headless
            ):
                print(result.text)
            if not self._options.stay or services.exited:
                return
            task = None

    def _teacher_tools(self, state: TurnState) -> list:
        """配置了 ``ai.teacher_model`` 时装配 ``ask_teacher``；与交互式同一套。"""
        config = load_config()
        ai = getattr(config, "ai", None)
        teacher_model = getattr(ai, "teacher_model", None) if ai else None
        if not teacher_model:
            return []
        try:
            provider, name = self._catalog.resolve(teacher_model)
        except Exception:  # noqa: BLE001 - 教师配置坏了不该挡住整次运行
            return []
        return teacher_tools(provider, name, lambda: state.transcript)


async def _paint(screen: PyteScreenModel) -> None:
    """把当前屏幕画到 stdout：整屏重绘，不接管终端。"""
    while True:
        await asyncio.sleep(PAINT_INTERVAL)
        lines = screen.text(limit=ROWS).splitlines()[-ROWS:]
        lines = [""] * (ROWS - len(lines)) + lines
        out = ["\x1b[H"]
        for line in lines:
            out.append(line + "\x1b[K\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()


def _budget_for(catalog: Any, provider_id: str) -> int:
    try:
        config = catalog.provider_config(provider_id)
    except Exception:  # noqa: BLE001 - 配置缺失时用默认窗口
        return default_budget(128_000)
    return default_budget(config.context_window, catalog.context_limit())


def _open_session(options: _Options, catalog: Any) -> Any:
    keeper = store()
    session = None
    if options.session:
        session = keeper.open(options.session)
        if session is None:
            raise SessionError(f"找不到会话：{options.session}")
        keeper.set_current(session.id)
        return session
    provider_id, model_id = catalog.resolve_spec()
    title = options.task.splitlines()[0][:40] if options.task else ""
    return keeper.create(provider_id, model_id, title=title)


def _load_handler(spec: str) -> Any:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("handler must be module:Class")
    handler = getattr(importlib.import_module(module_name), attribute)()
    if not hasattr(handler, "step"):
        raise TypeError(f"{spec} is not an LLS handler")
    return handler


def _make_recorder():
    config = load_config()
    if not config.recording.enabled:
        return None
    from .recording import TerminalRecorder

    rec = config.recording
    return TerminalRecorder(
        config.paths.recordings_dir,
        rows=ROWS,
        columns=COLUMNS,
        compress=rec.compress,
        max_recording_bytes=rec.max_recording_bytes,
        max_total_bytes=rec.max_total_bytes,
        fsync=rec.fsync,
    )


async def _run_external(options: _Options) -> int:
    handler = _load_handler(options.handler)
    handlers = {"main": handler}
    control_methods = ("pause", "resume", "step_once")
    if all(hasattr(handler, name) for name in control_methods):
        handlers["control"] = ControlHandler(handler)
    recorder = _make_recorder()
    screen = PyteScreenModel(rows=ROWS, columns=COLUMNS)
    interactive = (not options.headless and sys.stdin.isatty()
                   and sys.stdout.isatty())
    engine = Engine(
        make_backend(),
        options.command,
        screen,
        handlers,
        "main",
        recorder=recorder,
        attach_host=interactive,
    )
    try:
        await engine.run()
    finally:
        if recorder is not None:
            try:
                recorder.close()
            except OSError as e:
                print(f"lls serve: 录制收尾失败：{e}", file=sys.stderr)
        if engine.recorder_error:
            print(f"lls serve: {engine.recorder_error}", file=sys.stderr)
    return 0


async def _run(options: _Options) -> int:
    if options.handler != "chat":
        return await _run_external(options)
    try:
        catalog = default_catalog()
    except ConfigError as e:
        print(f"lls serve: {e}", file=sys.stderr)
        return 1
    reset_global_policy(catalog.tools())

    session = None
    try:
        session = _open_session(options, catalog)
        provider = catalog.get(session.meta.provider_id)
    except (SessionError, ConfigError) as e:
        if session is not None:
            session.close()
        print(f"lls serve: {e}", file=sys.stderr)
        return 1

    recorder = _make_recorder()

    screen = PyteScreenModel(rows=ROWS, columns=COLUMNS)
    handler = _Serve(
        options,
        session,
        provider,
        _budget_for(catalog, session.meta.provider_id),
        catalog,
    )
    engine = Engine(
        make_backend(),
        options.command,
        screen,
        {"serve": handler},
        "serve",
        recorder=recorder,
        attach_host=False,
    )
    if recorder is not None:
        # 会话事件指着录制的帧：回放时能对上「那一刻终端是什么样」。
        store().set_terminal(
            lambda: None if engine.recorder_error else recorder.current_ref
        )

    painter = None
    try:
        if not options.headless:
            sys.stdout.write("\x1b[2J\x1b[H")
            painter = asyncio.ensure_future(_paint(screen))
        await engine.run()
        if (
            not options.headless
            and not options.stay
            and handler.status == "ok"
            and handler.text
        ):
            # 有头时 stdout 归画面；答复在收尾时打一次，不与重绘抢位置。
            print()
            print(handler.text)
    finally:
        if painter is not None:
            painter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await painter
            sys.stdout.write("\n")
            sys.stdout.flush()
        if recorder is not None:
            store().set_terminal(None)
        store().write_meta(session.meta)
        session.close()
        if recorder is not None:
            try:
                recorder.close()
            except OSError as e:
                print(f"lls serve: 录制收尾失败：{e}", file=sys.stderr)
        if engine.recorder_error:
            print(f"lls serve: {engine.recorder_error}", file=sys.stderr)

    return 0 if handler.status == "ok" else 1


def main(argv: list[str] | None = None) -> int:
    options = _parse(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_run(options))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
