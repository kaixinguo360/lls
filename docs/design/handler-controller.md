# Handlers as controllers

LLS has one runtime and one controller contract.

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

A handler is the controller. `Engine` repeatedly calls:

```python
await handler.step(services)
```

There is no separate Session, Runtime, or controller loop.

## Loading a handler

```bash
lls serve --controller pkg.mod:Class -- /bin/bash
lls serve --controller chat
```

`--controller` instantiates the class with no arguments and installs it as the
Engine handler. The `chat` default keeps the existing `_Serve` path.

## Contract

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

Handlers that integrate a model or another runtime live outside LLS core and
are loaded by `--controller`; LLS does not ship a model adapter.
