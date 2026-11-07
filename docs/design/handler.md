# Handlers

LLS has one terminal runtime and one handler contract.

```
Engine
  ├── PTY
  ├── ScreenModel
  ├── Recorder
  └── Handler
        ├── PassthroughHandler   human
        ├── CommandHandler       human
        └── _Serve               chat
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

## Contract

```python
class Handler(Protocol):
    async def step(self, services: Services) -> None: ...
```

The handler decides how long to wait. Tick policies call
`await services.wait(0.1)` themselves; chat turns block for as long as needed.

`Services` is the existing app-level API: screen, write, read_line, overlay,
tools, sessions.

External handlers own their input and control behavior. LLS does not define a
control plane, pause protocol, passthrough flag, or Ctrl-E binding for them.
