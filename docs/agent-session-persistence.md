# agent 会话持久化：按 session id 组织（内存级）

> 实现状态：内存级已完成 —— `commands/builtin/agent_store.py`（SessionState /
> SessionStore / store 单例）、`Catalog.resolve_spec()`、`_Session` 接 state 并在
> `_one()` 成功收尾时写回、`agent` 恢复 current、`agent --new` 新建。磁盘落盘也
> 已完成（见「落盘实现」）。
> 尚未实现（待定）：`agent <id>`、`--model`、会话内 `/sessions` `/use` `/new`。

## 现状

- `cmd_agent`（src/lls/commands/builtin/agent.py:703）每次 `_Session(...)` 新建，
  `_history` / `_log` 是实例属性（:589-600），退出即丢。
- 历史在出、入两处各 trim 到 `HISTORY_TURNS-1 = 3` 轮（`_one()`）。
- provider/model 每次 `default_catalog().resolve()` 重新解析。
- `Message` 是 frozen dataclass，可拷贝、可序列化。

## 为什么不该拿 provider/model 当 key

那是「一条会话不要重复配置」的省事写法，代价是：

1. 一个模型只能有一条会话，无法并行两条任务线（同一模型开两个上下文）；
2. 会话身份被模型绑死，换模型 = 换会话，想比较两个模型的同一任务得手动搬历史；
3. 没有地方挂 `title / created_at / 轮数` 这类会话元数据，`/sessions` 无从列起；
4. 将来落盘时文件名只能是 `<provider>-<model>.json`，同模型多会话无法表达。

正确模型：**会话是独立实体，带自己的 id**；provider/model 是会话的**属性**。

## 数据模型（新增 src/lls/commands/builtin/agent_store.py）

```python
@dataclass
class SessionState:
    id: str                       # "s1a2b3c4"，由 token_hex(3) 生成
    provider_id: str
    model_id: str
    history: List[Message] = field(default_factory=list)
    log: List[Tuple[str, str, str]] = field(default_factory=list)  # (task, reply, note)
    created_at: float = ...
    updated_at: float = ...

class SessionStore:
    def new(self, provider_id: str, model_id: str) -> SessionState
    def get(self, session_id: str) -> SessionState | None
    def list(self) -> List[SessionState]          # 按 updated_at 倒序
    def drop(self, session_id: str) -> None
    def current(self) -> SessionState | None      # 「上一次用的」
    def set_current(self, session_id: str) -> None
    def trim(self, keep: int = 8) -> None         # 超量按 updated_at 淘汰

def store() -> SessionStore    # 进程内单例
```

- id 用 `secrets.token_hex(3)`（8 字符，无新依赖），碰撞概率可忽略，肉眼可读。
- 上限按会话数（默认 8）而非按模型数；`set_current` 存 id 而非对象。
- key 是 id，`provider_id/model_id` 只做字段：换模型不丢会话，切模型可就地改。

## 命令面

| 入口 | 行为 |
|---|---|
| `agent` | 恢复 `current()` 会话；没有就新建。provider/model 用**会话记录的值** |
| `agent --new` | 新建会话并设为 current |
| `agent <id>` / `agent --session <id>` | 恢复指定会话（校验存在，不存在给提示） |
| `agent --model P/M ...` | 在会话上换模型（旧历史保留，元数据更新） |
| 会话内 `/new` | 新建并切走，旧会话留在 store |
| 会话内 `/sessions` | 列 `id  轮数  模型  相对时间  首条任务` |
| 会话内 `/use <id>` | 切到另一条会话继续 |
| 会话内 `/exit` | 只是退出，会话保留 |

提示行显示 `(agent s1a2b3c4 · 3 轮): `，让「接着上次」可见；`--new` 后 id 变化即证明是新的。

## 接线

- `_Session.__init__(services, provider, model, state)`：`self._state = state`，
  `self._history = list(state.history)`、`self._log = list(state.log)`。
- 写回只在 `_one()` 成功收尾处：`state.history = self._history`、
  `state.log = self._log`、`state.updated_at = time.time()`。
  Ctrl-C、异常、`_SessionEnded` 不写（沿用「半成品不入历史」）。
- `cmd_agent` 只做参数解析 + 取/建 `SessionState` + `store().set_current(state.id)`。
- provider/model 解析：会话有记录就 `catalog.get(state.provider_id)`（provider 已删则报错并提示 `--new`）；
  未指定时回落到 `default_catalog()` 的 `default_model`。
  需要 provider_id：给 `Catalog` 加 `resolve_spec(spec) -> (provider_id, model_id)`，`resolve()` 复用它。

## 落盘实现

- 目录：`$LLS_SESSIONS_HOME/sessions`，默认 `~/.local/lls/sessions`（不挂
  `~/.local/state`，与 `~/.config/lls` 对称）；目录 0700、文件 0600。
  旧位置的 `$XDG_STATE_HOME/lls/sessions` 在第一次用到新目录时被复制一次。
- 一条会话一个 `<id>.json`（`version / id / provider_id / model_id / created_at /
  updated_at / history / log`），`index.json` 只存 `current`；`Message` 的
  `content`、`tool_calls`、`tool_call_id` 原样往返。
- 写：同目录 `.tmp` + `os.replace`；失败记进 `SessionStore.last_error`，不抛给
  刚完成一轮的会话。
- 读：构造 `SessionStore(root=...)` 时全量载入；坏 JSON、版本不符、字段不对、
  文件名不是一个合法 id 的，一律跳过。内存上限（`MAX_SESSIONS`）同样作用于磁盘，
  淘汰即删文件。
- 写回点不变：仍只在 `_one()` 成功收尾时 `store().save(state)`；`new` / `drop` /
  `set_current` 各自落盘。

## 边界

- 会话跨 lls 重启保留；坏文件跳过，写失败只记录（终端里照常继续）。
- 落盘时天然一对一：`~/.local/lls/sessions/<id>.json`，一个会话一个文件，
  `Message` 字段直映射，临时文件 + `os.replace` 原子写，读损坏就跳过该文件。
  `current` 另存 `sessions/index.json`。上层代码零改动。
- 命名注意：`lls/session/` 是终端引擎包，与此处「agent 会话」无关；
  store 放在命令层（`commands/builtin/agent_store.py`）。
  若日后 `generate` 也要共享历史，再把 `history` 上提到 `lls/ai/sessions.py`。

## 改动清单

| 文件 | 改动 |
|---|---|
| `src/lls/commands/builtin/agent_store.py` | 新增，约 110 行 |
| `src/lls/commands/builtin/agent.py` | `_Session` 收 `state`；`_one()` 写回；`/new` `/sessions` `/use`；`cmd_agent` 参数解析 |
| `src/lls/ai/catalog.py` | 加 `resolve_spec()`，`resolve()` 复用之（约 10 行） |
| `README.md` | 命令表、agent 一节说明会话延续与切换 |
| `tests/unit/test_agent_session_store.py` | 新增；复用 `test_agent_session.py` 的 stub provider |

## 测试要点

1. 两次 `agent`：第二次请求含上一轮任务与答复；提示里有同一个 id。
2. `--new` / `/new` 后请求不含旧消息，旧会话仍在 `store().list()` 里且可 `/use` 回来。
3. 同模型两条会话互不串味：A、B 交替 `/use`，各自历史只增自己的。
4. `--model` 换模型后历史保留；provider 被删除时给可读错误。
5. Ctrl-C 中止的一轮不写回；`/exit` 后 `current()` 不变。
