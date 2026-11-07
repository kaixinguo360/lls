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
