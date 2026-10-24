"""组合根：装配 Settings / Screen / Session / Controller 并运行。"""

from __future__ import annotations

import asyncio
import os
import sys

from .ai import Session
from .settings import Paths, _xdg_config_dir, default_settings, load_settings
from .session.controller import Controller
from .term import PyteScreenModel
from .tty import make_backend

__all__ = ["run_interactive"]


def run_interactive(command: list[str], *, config: str | None = None, role: str | None = None) -> int:
    config_dir = _xdg_config_dir()
    paths = Paths(config_dir)
    config_file = paths.config_file if config is None else config

    settings = load_settings(config_file)
    if role and role in settings.roles:
        settings = settings.__class__(settings.ai, settings.roles, role)

    try:
        size = os.get_terminal_size()
    except OSError:
        size = os.terminal_size((80, 24))
    screen = PyteScreenModel(rows=size.lines, columns=size.columns)

    session = Session(id="main", role_id=settings.role_id)
    backend = make_backend()
    controller = Controller(backend, command, screen, settings, session, paths)

    try:
        return asyncio.run(controller.run())
    except RuntimeError as e:
        print(f"lls: {e}", file=sys.stderr)
        return 1
