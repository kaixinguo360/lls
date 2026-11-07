from __future__ import annotations

import asyncio

from lls.headless import _load_handler


class FakeServices:
    def __init__(self):
        self.writes: list[bytes] = []
        self.waits: list[float] = []

    def write(self, data: bytes):
        self.writes.append(data)

    async def wait(self, seconds: float):
        self.waits.append(seconds)


def test_external_handler_is_loaded_and_steps():
    handler = _load_handler("tests.unit.support_handlers:EchoHandler")
    services = FakeServices()
    asyncio.run(handler.step(services))
    assert services.writes == [b"echo handler-ready\r"]
    assert services.waits == [0.05]
