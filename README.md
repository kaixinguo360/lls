# LLS

基于 PTY 的终端包装器：VT100 屏幕模型 + 覆盖层界面。

> 当前处于重构中。旧实现见 [`legacy/`](legacy/)，新实现见 [`src/lls/`](src/lls/)。

## 结构

```
src/lls/session/    终端引擎与它对外的能力接口
src/lls/handlers.py 输入处理器（透传 / 命令分发）
src/lls/commands/   命令层，按类别分模块
src/lls/ai/         AI 模块：配置、provider 抽象、会话与 agent 循环
src/lls/term/       VT100 屏幕模型（pyte 适配）
src/lls/ui/         显示原语（换行、覆盖层定位、按模型渲染）
src/lls/edit/       行编辑缓冲
src/lls/tty/        PTY 与平台终端后端（按键编解码）
```

引擎不认识任何命令：主循环只反复调用当前处理器的 `step()`。新增一种输入
方式 = 新增一个处理器；新增一条命令 = 加一个带 `@command` 的函数。

`src/lls/ai/` 不认识命令和终端，只做「消息进、增量出」的协议转发；想让
`generate` 用上别的后端，在 `ai/providers/` 加一个模块并注册即可。通用
agent 循环（`ai/agent.py`）也只驱动「请求 → 工具 → 请求」，具体工具由命令层
提供。

## 命令

Ctrl-E 进入命令模式，输入命令名。

| 命令 | 别名 | 说明 |
|---|---|---|
| `agent` | `a` | 进入交互式 Agent 会话，按任务在终端里自动多轮执行 |
| `generate` | `g`, `gen` | 由终端状态与请求生成一段输入，确认后写入子进程 |
| `exec` | `e` | 执行一条命令，末尾附加回车 |
| `input` | `i` | 写入内容但不附加回车 |
| `help` | `h`, `?` | 列出所有可用命令 |
| `err` | | 显示最近捕获的错误信息 |
| `quit` | `q`, `exit` | 退出命令模式，回到透传 |

`generate 请求#内容` 跳过生成，直接把 `#` 后面的内容交给确认菜单；手写的内容
同样过一眼再发。

## agent 会话

Ctrl-E 后输入 `agent`（或带首个任务：`agent 把测试跑一遍`）进入交互式 Agent
会话。每次输入一行任务，模型就在终端里自动多轮执行：调用工具、读终端、再
决定下一步，直到它用一条**不调用工具**的回复收尾；随后回到同一个提示，继续
安排下一个任务。历史跨任务保留（最近几轮会随请求带上）。

会话提示里显示最近两轮的任务与答复（每轮答复最多几行，避免覆盖层过高）。
空行或 Ctrl-C 清掉当前输入；Esc、Ctrl-D 或 `/exit` 退出会话回到透传。执行中
按 Ctrl-C 只中止当前任务，会话继续；半成品不会写入历史。想直接操作 shell 时
按 Esc 退出；再次 `agent` 是新会话（终端画面本身就是上下文）。

工具：
- `read_terminal`：读取终端最近若干行，可等待输出静默；什么时候读、等多久由
  模型决定（动作工具不会自动回显画面）。
- `send_text`：写入文字；`enter=true` 时作为命令提交。
- `send_keys`：具名按键（enter/tab/esc/方向键/`ctrl-c`/`f1`…/`alt-x`）。
- `send_raw`：以 base64/hex 发送任意字节（表里没有的序列、非 UTF-8 字节）。

不限制步数：任务会一直自动执行，直到模型给出文本答复，或你按 Ctrl-C 中止。
没有固定上限的代价是长任务会持续累积上下文（每轮都带完整历史），请留意终端
状态与模型输出。运行期间用户按键只用于 Ctrl-C，其余按键丢弃；终端对子进程查询
（光标位置、设备属性等）的应答会照常转交给子进程，不会丢。`agent` 不弹确认，
模型给出的输入会直接写入子进程，请在你愿意让它操作的终端里使用。

## 配置

`generate` 和 `agent` 从 YAML 读 provider 配置，路径优先级：
`LLS_CONFIG` > `$XDG_CONFIG_HOME/lls/config.yaml` > `~/.config/lls/config.yaml`。

```yaml
ai:
  default_model: openai/gpt-4o-mini
  providers:
    openai:
      type: openai
      base_url: https://api.openai.com/v1
      api_key: sk-...
      models: [gpt-4o-mini, gpt-4o]
    local:
      type: openai
      base_url: http://localhost:11434/v1
      api_key: ollama
      models: [qwen3-coder]
```

`default_model` 用 `provider_id/model_id` 寻址，模型名本身可含斜杠
（只按第一个斜杠切分）。用 `pip install 'lls[ai]'` 装 `openai` 依赖。

`params` 里的键会原样进请求体（`temperature` 之类）：

```yaml
      params:
        temperature: 0.2
```

## 开发

```bash
# 运行测试（需先 pip install -e ".[dev]"）
python -m pytest

# 语料回放
python tools/replay.py replay <corpus.raw>
python tools/replay.py compare <corpus.raw>
```

详见 [docs/](docs/)（待补）与 [tests/corpora/README.md](tests/corpora/README.md)。
