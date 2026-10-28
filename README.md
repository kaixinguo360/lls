# LLS

基于 PTY 的终端包装器：在真实 shell 之上提供 VT100 屏幕模型与一层浮动的
命令界面，可以读取终端状态、生成命令、让模型直接操作终端。

- **透传优先**：默认就是普通终端，子进程独占输入输出。
- **命令层**：`Ctrl-E` 进入命令模式，命令以 `(services, args)` 的形式实现。
- **屏幕模型**：从字节流维护一份可读的屏幕状态（备屏、颜色、光标、回滚区），
  供命令读取，也用于重绘。
- **AI 可选**：`generate` 与 `agent` 通过可插拔的 provider 取模型，未配置时
  其余功能照常可用。

## 安装

需要 Python 3.8+ 与 POSIX 终端（Linux / macOS / WSL）。

```bash
pip install -e .            # 仅核心功能
pip install -e ".[ai]"      # 额外安装 openai 包（也可改用 openai-http，不装）
lls --doctor                # 检查环境
```

```bash
lls             # 在 $SHELL 里启动
lls bash        # 指定命令
```

非交互环境（stdin 不是 tty）下 `lls` 不接管终端，直接执行命令。

## 命令

`Ctrl-E` 进入命令模式，回车或 Esc 回到透传。

| 命令 | 别名 | 说明 |
|---|---|---|
| `agent` | `a` | 进入交互式 Agent 会话，按任务在终端里自动多轮执行 |
| `generate` | `g`, `gen` | 由终端状态与请求生成一段输入，确认后写入子进程 |
| `exec` | `e` | 执行一条命令，末尾附加回车 |
| `input` | `i` | 写入内容但不附加回车 |
| `help` | `h`, `?` | 列出所有可用命令 |
| `err` | | 显示最近捕获的错误信息 |
| `quit` | `q`, `exit` | 退出命令模式，回到透传 |

命令拿到的是完整的 `Services`：读屏幕、画覆盖层、读一行、写子进程都是原语，
命令自己决定流程。

### generate

`generate` 读一条请求（或直接带上：`generate 查看日志`），把最近的终端画面
与请求交给模型，产出确认后写进子进程：

```
(gen-instruct): 找到占用 8080 的进程
(gen-cmd): lsof -i :8080, confirm? [y/i/n/e/r]
```

`y` 写入并回车，`i` 写入但不回车，`n` 放弃，`e` 改写请求，`r` 重新生成。
模型返回空内容同样是正常结果，会进入确认菜单。

`generate 请求#内容` 跳过生成，只走确认菜单 —— 手写的内容也过一眼再发。

一次 `lls` 运行就是一段会话：每轮记下当时的终端画面、请求与结果，下一次生成
把这些一起发给模型（最近若干轮）。

### agent

`agent` 进入交互式会话，每行一个任务；模型在终端里反复「调用工具 → 看结果」
直到给出文本答复，然后回到同一个提示，接着安排下一个任务。

工具：

- `read_terminal`：读取终端最近若干行，可等待输出静默；
- `send_text`：写入文字，`enter=true` 时作为命令提交；
- `send_keys`：具名按键（`enter`/`tab`/`esc`/方向键/`ctrl-c`/`f1`…/`alt-x`）；
- `send_raw`：以 base64/hex 发送任意字节。

空行或 `Ctrl-C` 清掉当前输入；`Esc`、`Ctrl-D` 或 `/exit` 退出回到透传。
执行中按 `Ctrl-C` 只中止当前任务，会话继续。任务没有步数上限，随时可以
`Ctrl-C` 中止。

会话有独立身份并落盘到配置的 `paths.sessions_dir`（默认 `~/.local/lls/sessions`），
一条会话一个 JSON 文件。再次 `agent` 回到同一段上下文，`agent --new` 另开一条。

`agent` 不弹确认，模型给出的输入直接写入子进程 —— 请在愿意让它动手的终端里
使用。

## 输入历史

提示行里按 `↑`/`↓` 可以召回以前敲过的内容，重启后仍在。历史是 JSONL，
按 `history_id` 分文件：命令行、`exec`/`input` 这些是本机共用的一份
（`global/`），agent 的任务描述、`generate` 的指令跟着当前会话走
（`session-<id>/`，换会话换一份，会话删掉一起删）。文件权限 0600、目录
0700。

- `input_history.enabled: false`：不落盘，`↑` 只在本次进程里有效；
- `paths.history_dir`：历史根目录，默认 `paths.root/history`；写绝对路径可单独挪走；
- `input_history.max_entries`：载入进 `↑` 的条数上限，默认 200；
- `input_history.max_bytes`：单个历史文件超过它就顺手压缩，默认 131072。

写不进去（家目录只读、磁盘满）只是不再落盘，不影响继续输入。

## 配置

配置路径优先级：`--config` > `LLS_CONFIG` > `$XDG_CONFIG_HOME/lls/config.yaml`
> `~/.config/lls/config.yaml`。`--set KEY=VALUE` 可临时覆盖一项，如
`--set input_history.max_entries=50`，走与 YAML 相同的解析路径。

文件不存在或写坏时，除 AI 命令外都用内置默认值照常启动；未知键会告警，老键名
（`state` / `history` / `history.limit`）会提示改成了哪个。`paths` 里的相对路径
一律相对配置文件所在目录。`--doctor` 会打印实际生效的路径。

```yaml
paths:
  root: ~/.local/lls
  sessions_dir: sessions      # 相对配置文件所在目录；也可写绝对路径
  history_dir: history

input_history:
  enabled: true
  max_entries: 200
  max_bytes: 131072

ai:
  default_model: openai/gpt-4o-mini
  providers:
    openai:
      type: openai
      base_url: https://api.openai.com/v1
      api_key: sk-...
      models: [gpt-4o-mini, gpt-4o]
    local:
      type: openai-http
      base_url: http://localhost:11434/v1
      api_key: ollama
      models: [qwen3-coder]
```

`default_model` 用 `provider_id/model_id` 寻址；模型名本身可含斜杠，只按第一个
斜杠切分。

`type` 决定传输层，两者说同一种协议：

| type | 实现 | 依赖 |
|---|---|---|
| `openai` | 通过 `openai` 包 | 需要 `pip install 'lls[ai]'` |
| `openai-http` | 标准库直接发 HTTP/1.1、自己解 SSE | 无 |

`openai-http` 走 `asyncio` 直连（http/https 皆可），流式输出期间界面照常
刷新，`Ctrl-C` 立即断开。对后端的要求与 SDK 一致：`POST {base_url}/chat/completions`，
流式 SSE。

`params` 里的键会原样进请求体：

```yaml
      params:
        temperature: 0.2
```

## 结构

```
src/lls/app.py           组合根：装配引擎与处理器
src/lls/session/         终端引擎与对外的能力接口（Services）
src/lls/handlers.py      输入处理器（透传 / 命令分发）
src/lls/commands/        命令层，内置命令按类别分模块
src/lls/ai/              provider 抽象、多轮对话、通用 agent 循环
src/lls/term/            屏幕模型（pyte 适配）
src/lls/ui/              显示原语：换行、覆盖层定位、按模型渲染、字符宽度
src/lls/edit/            行编辑缓冲
src/lls/tty/             PTY 与终端后端、按键编解码
```

依赖方向是单向的：引擎不认识命令；`commands` 只依赖 `Services`；`ai` 不认识
终端。新增一种输入方式 = 一个处理器；新增一条命令 = 一个带 `@command` 的函数；
新增一个模型后端 = `ai/providers/` 里的一个模块。

## 平台

判据是 `pty` / `termios` / `fcntl` 三个模块是否真的可用，而不是平台名。
Windows 原生没有这些模块，请使用 WSL；`lls --doctor` 会给出结论。

## 开发

```bash
pip install -e ".[dev]"

python -m pytest        # 测试
ruff check src tests    # 静态检查
mypy                    # 类型检查（term 与 edit 两个包为严格模式）
```

代码需要在 3.8 上可导入：注解一律依赖 `from __future__ import annotations`，
赋值语句里的类型则使用 `typing` 的形式（`tests/unit/test_py38_compat.py`
会静态检查这一点）。
