"""Handlers used by tests and headless smoke runs."""

from __future__ import annotations


class EchoHandler:
    """Write one harmless command, then idle."""

    def __init__(self) -> None:
        self.sent = False

    async def step(self, services) -> None:
        if not self.sent:
            self.sent = True
            services.write(b"echo handler-ready\r")
        await services.wait(0.05)


class PauseHandler:
    """Minimal controllable handler for --control smoke tests."""

    def __init__(self) -> None:
        self.paused = False

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def step_once(self) -> None:
        self.paused = True

    async def step(self, services) -> None:
        await services.wait(0.1)
