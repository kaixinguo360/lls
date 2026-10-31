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

`Ctrl-E` 进入命令模式，回车或 Esc 回到透传。命令名与参数支持 Tab 补全，
默认是**行尾虚影**：输入后面用灰字显示还没打的那一截与用法参数，`Tab` 或
`→` 接受。写 `input_completion.style: redis` 可换成 redis-cli 式轮盘：第一个
候选先预览在行上，再 Tab 原地轮换，Esc 取消、其它键接受。

| 命令 | 别名 | 说明 |
|---|---|---|
| `agent` | `a` | 进入交互式 Agent 会话，按任务在终端里自动多轮执行 |
| `sessions` | `ls` | 列出会话（`--all` 含已归档） |
| `fork` | | 从某条会话的某个位置分叉：`fork <id> [--at <seq>]` |
| `rm` | | 删除一条会话（连同它的输入历史） |
| `generate` | `g`, `gen` | 由终端状态与请求生成一段输入，确认后写入子进程 |
| `exec` | `e` | 执行一条命令，末尾附加回车 |
| `input` | `i` | 写入内容但不附加回车 |
| `help` | `h`, `?` | 列出所有可用命令 |
| `err` | | 显示最近捕获的错误信息 |
| `quit` | `q`, `exit` | 退出命令模式，回到透传 |

命令拿到的是完整的 `Services`：读屏幕、画覆盖层、读一行、写子进程都是原语，
命令自己决定流程。

覆盖层跟着终端尺寸走：窗口一改，引擎按新宽度重画它（提示行按原文重新折行，
消息框重画记下的文本，超过一屏的部分截在屏底）。自己排版的界面（如翻看器）
用 `set_overlay_source` 交出「按新行列数重排」的回调，收起时传 `None`。

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
- `send_raw`：以 base64/hex 发送任意字节；
- `wait_for`：等到画面出现某段文字（正则，只在之后新出现的行里找）或安静下来，
  慢命令用它一次等到，不必反复读屏；
- `bash`：在另一个 shell 里跑一条命令，回退出码与完整输出（`cd` 留到下次
  调用；它看不见用户终端，也驱动不了 TUI）；
- `read_file`：按行号读磁盘上的文本文件；
- `write_file`：直接写文件（`append=false` 整体替换），适合改配置、写脚本。

空行或 `Ctrl-C` 清掉当前输入；`Esc`、`Ctrl-D` 或 `/exit` 退出回到透传。
执行中按 `Ctrl-C` 只中止当前任务，会话继续。任务没有步数上限，随时可以
`Ctrl-C` 中止。执行中也一样能打字：屏幕默认只有那一行状态，按第一下键
底下才摊开输入行，提示从 `(agent)` 变成 `(agent-queued)` —— 这一行回车是
排队，排在这一轮之后（一行先整行解析，命令也解析）。输入行一空就收起，回到
单行。`Ctrl-C` 只弃这一轮，草稿留着。

忙碌只是一个状态位，不是另一套界面：斜杠命令在这一轮跑着时照样可用。行首敲
下 `/` 提示符就换成 `(agent-cmd)`（执行中也一样，命令就是命令），回车是
执行命令而不是下一轮任务。`/` 命令同样支持补全：补出来的是规范名
（`/ls` 会补成 `/sessions`），默认的行尾虚影同时给还没打的那一截与用法。命令按「会不会碰到本轮正在用的东西」分四档：
`/help` `/s` `/tools` `/title` `/model` 就地执行，做完回到执行中界面；
`/compact` 排到本轮结束后；`/undo` `/retry` `/sessions` `/new` `/fork` `/rm`
`/archive` 执行中拒绝，并提示先 `Ctrl-C`；`/exit` 先让本轮收尾再退出。

会话有独立身份，落盘到配置的 `paths.sessions_dir`（默认 `~/.local/lls/sessions`）。
一条会话一个目录，`events.jsonl` 是唯一事实来源：一行一事件，只追加，
`meta.json` 只放列表要用的元信息。崩溃最多丢最后一行，不会把整条会话弄坏。

```
sessions/
  index.json                  当前会话的缓存，丢了按目录重扫
  20251028-1052-7d1075/
    meta.json                 标题、模型、cwd、轮数、大小、更新时间
    events.jsonl              session_start / screen / user / assistant / tool /
                              turn_end / compact
    blobs/<sha1>              超过 8KB 的工具输出与画面正文，事件里只留指针
    lock                      占用者的 fcntl 排他锁
```

送进模型的上下文是**按预算从事件里派生**的只读视图，与磁盘上留了多少无关：

- 预算 = `min(context_window * 0.6, ai.context_limit)`，按 token 估算，
  优先用 provider 返回的 `usage`；
- 从尾部向前装，按 `user` 边界切分，`tool_calls` 与对应 `tool` 回复不拆；
- 每轮随任务发出去的终端画面单独落一条 `screen`，`user` 指向它：重放出来的
  就是当时发出去的那条消息（逐字相同，缓存的前缀才连得上），画面没留下就
  退回纯任务；
- 每轮只读最近一次压缩之后的事件，压缩过的前缀不再逐行解码；
- 中断、出错的轮次照样进上下文：没等到结果的 `tool_calls` 补一条 `error: …`，
  中断的那一轮再补一行「被用户中断」的标记 —— 下一轮看得见上一轮停在哪儿，
  不会把做过的事情再做一遍；
- 用量到预算的 85% 就把最旧的一段压成摘要、压回预算的 60%（`compact` 事件，原文
  仍在 events 里），两次压缩之间因此隔得开几轮；新摘要以旧摘要为底稿合并；
  摘要失败才退回硬截断，压掉的太少就不压 —— 摘要请求本身也要花钱。

会话内以 `/` 开头的是本地命令，不发给模型（`/help` 列全部）：`/status`（别名 `/s`）
整屏翻看全部对话（打开即停在最后一行，往上翻看历史；版式与提示里 `(agent)`/`(agent-reply)`/`(agent-note)` 一致），
`↑`/`↓`（或 `j`/`k`）、空格/`b` 翻页、`PgUp`/`PgDn`、`g`/`G` 与 `Home`/`End` 跳到首尾，
`q`、Esc 或 Ctrl-C 收起，末行是标题·模型·用量；`/title <文字>`
改标题，`/model [spec]` 换模型，`/compact` 立刻压缩历史，`/tools` 列工具，
`/sessions`、`/resume <id>`、`/new [标题]`、`/fork [--at <seq>]` 管会话，
`/rm` 删掉本会话、`/archive` 把它收进 `sessions/_archive/`（列表里不再出现，
`--all` 仍可见）—— 两个都要先确认，办完不退出，而是把剩下的会话摆出来让你挑
一条接着用（不顺手再开一条空的；一条都不剩、或者按 `q` 才退出 agent），
`/retry` 接着本轮继续执行（不加任何新消息，等同于这一轮没结束；接着跑出来的
答复在提示里另起一条，中断前那句也还留着）。没匹配上的 `/xxx` 直接报错而不是当任务发出去 —— 打错的命令
变成指令代价更大；确实要发就用 `//xxx` 转义，`/usr/bin/env` 这类路径也照原样发出。

再次 `agent` 回到同一段上下文；`agent --new [任务]` 另开一条；
`agent --resume <id|前缀|标题片段>` 显式恢复；`agent --list [--all]` 列会话；
`agent --title <文字>`、`agent --model <spec>` 换标题与模型（换模型记一条
`model_switch`）。另一进程正开着同一条会话时不会硬抢，会提示换一条。

超过 `MAX_SESSIONS`（20）条的会话不删，移到 `sessions/_archive/`，`--all` 仍可见；
真删用会话内的 `/rm`（要确认，删完就地挑一条会话接着用），只想收起来就用
`/archive`。

`agent` 不弹确认，模型给出的输入直接写入子进程 —— 请在愿意让它动手的终端里
使用。

## 输入历史

提示行里按 `↑`/`↓` 可以召回以前敲过的内容，重启后仍在。历史是 JSONL，
按 `history_id` 分文件：命令行、`exec`/`input` 这些是本机共用的一份
（`global/`），agent 的任务描述、`generate` 的指令跟着当前会话走
（`session-<id>/`，换会话换一份，会话删掉一起删）。文件权限 0600、目录
0700。

agent 的对话行在同一份历史里分了两类：回车会跑命令的（行首 `/`，敲错的
也算）与发给模型的任务。提示符显示 `(agent-cmd)` 时 `↑` 只翻命令，其余
时候只翻任务 —— 敲了 `/` 按 ↑ 不会翻出一条任务话，反之亦然。`//转义` 与
`/usr/bin/env` 这类路径仍算任务，与提示符的判据是同一条。

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

input_completion:
  style: ghost                  # ghost：行尾虚影（Tab / → 接受）；redis：Tab 轮盘

ai:
  default_model: openai/gpt-4o-mini
  providers:
    openai:
      type: openai
      base_url: https://api.openai.com/v1
      api_key: sk-...
      models: [gpt-4o-mini, gpt-4o]
      context_window: 128000   # 窗口预算按它算，默认 128000
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

`ai.context_limit` 给整台机器一个硬上限（token），0 表示不设：
实际预算取 `min(context_window * 0.6, context_limit)`。

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
src/lls/ai/sessions/     会话落盘（事件流）、窗口裁剪与压缩
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
