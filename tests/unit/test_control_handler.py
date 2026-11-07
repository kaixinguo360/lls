from __future__ import annotations

import asyncio

from lls.handlers import ControlHandler


class Target:
    def __init__(self):
        self.paused = False
        self.single = False
        self.resumed = 0

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False
        self.single = False
        self.resumed += 1

    def step_once(self):
        self.paused = True
        self.single = True


class Services:
    def __init__(self, line):
        self.line = line
        self.handler = None
        self.stopped = False
        self.writes = []

    async def read_line(self, prompt, **kwargs):
        return self.line

    def set_handler(self, name):
        self.handler = name

    def write(self, data, enter=False):
        self.writes.append((data, enter))

    async def show_message(self, text):
        pass

    def stop(self):
        self.stopped = True


def run(line):
    target = Target()
    services = Services(line)
    asyncio.run(ControlHandler(target).step(services))
    return target, services


def test_pause_returns_to_main():
    target, services = run("pause")
    assert target.paused is True
    assert services.handler == "main"


def test_step_once_returns_to_main():
    target, services = run("step")
    assert target.paused is True
    assert target.single is True
    assert services.handler == "main"


def test_resume_returns_to_main():
    target, services = run("resume")
    assert target.paused is False
    assert target.resumed == 1
    assert services.handler == "main"


def test_quit_stops_engine():
    target, services = run("quit")
    assert services.stopped is True
    assert services.handler is None


def test_input_writes_without_enter():
    target, services = run("input echo hi")
    assert target.paused is True
    assert services.writes == [(b"echo hi", False)]
    assert services.handler == "main"


def test_exec_writes_with_enter():
    target, services = run("exec echo hi")
    assert target.paused is True
    assert services.writes == [(b"echo hi", True)]
    assert services.handler == "main"
