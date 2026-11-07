# Handlers and the control plane

LLS has one runtime and one handler contract.

```
Engine
  ├── PTY
  ├── ScreenModel
  ├── Recorder
  └── Handler
        ├── PassthroughHandler   human
        ├── CommandHandler       human
        ├── _Serve               chat
        └── external handler     model / bridge
```

`Engine` repeatedly calls:

```python
await handler.step(services)
```

There is no separate Session, Runtime, or controller loop.

## Running a handler

```bash
lls serve --handler pkg.mod:Class -- /bin/bash
lls serve --handler chat
```

`--handler` instantiates the class with no arguments and installs it as the
Engine handler. The `chat` default keeps the existing `_Serve` path.

With a TTY and without `--headless`, the handler runs under the normal LLS
HostUI: Renderer, overlays, resize and keyboard are active. Without a TTY, or
with `--headless`, it runs headless and still records LLS-v2.

## Handler contract

```python
class Handler(Protocol):
    async def step(self, services: Services) -> None: ...
```

The handler decides how long to wait. Tick policies call
`await services.wait(0.1)` themselves; chat turns block for as long as needed.

`Services` is the existing app-level API: screen, write, read_line, overlay,
tools, sessions. A model handler should use only:

```python
services.screen
services.write(bytes)
services.wait(seconds)
services.stop()
```

and should not call `read_line`, overlays, or tools.

## Control plane

A handler that implements pause/resume/step_once/set_passthrough gets a
`ControlHandler` next to the main handler automatically:

```text
main handler
  Ctrl-E → services.set_handler("control")

ControlHandler
  pause | resume | step | quit
  input <text> | exec <command>
  passthrough on|off
  ? | help
  q / Esc → back to main
```

Normal host keys are passed through to the child by default, so human and
model input interleave. `passthrough off` stops forwarding normal keys;
`passthrough on` restores it. `Ctrl-E` is always intercepted.

`ControlHandler` only pauses/resumes/steps the target, plus explicit one-shot
writes via `input`/`exec`; it does not run app commands or use tools. `input`
writes text without Enter; `exec` writes the command and Enter. Both pause the
target first and leave it paused.

The target handler must provide:

```python
def pause(self) -> None: ...
def resume(self) -> None: ...
def step_once(self) -> None: ...
def set_passthrough(self, enabled: bool) -> None: ...
```

A tick handler consumes host keys itself, forwards them when passthrough is on,
and switches to `control` on Ctrl-E. `Ctrl-E` never reaches the child or the
recorder.

External model handlers live with the model and are loaded with `--handler`;
LLS does not ship a model adapter.
