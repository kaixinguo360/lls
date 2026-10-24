# LLS

基于 PTY 的终端包装器：VT100 屏幕模型 + 覆盖层界面。

> 当前处于重构中。旧实现见 [`legacy/`](legacy/)，新实现见 [`src/lls/`](src/lls/)。

## 结构

```
src/lls/session/    终端引擎与它对外的能力接口
src/lls/handlers.py 输入处理器（透传 / 命令分发）
src/lls/commands/   命令层，按类别分模块
src/lls/ai/         AI 模块：配置、provider 抽象、实例目录
src/lls/term/       VT100 屏幕模型（pyte 适配）
src/lls/ui/         显示原语（换行、覆盖层定位、按模型渲染）
src/lls/edit/       行编辑缓冲
src/lls/tty/        PTY 与平台终端后端
```

引擎不认识任何命令：主循环只反复调用当前处理器的 `step()`。新增一种输入
方式 = 新增一个处理器；新增一条命令 = 加一个带 `@command` 的函数。

`src/lls/ai/` 不认识命令和终端，只做「消息进、增量出」的协议转发；想让
`generate` 用上别的后端，在 `ai/providers/` 加一个模块并注册即可。

## 命令

Ctrl-E 进入命令模式，输入命令名。

| 命令 | 别名 | 说明 |
|---|---|---|
| `generate` | `g`, `gen` | 由终端状态与请求生成一段输入，确认后写入子进程 |
| `exec` | `e` | 执行一条命令，末尾附加回车 |
| `input` | `i` | 写入内容但不附加回车 |
| `help` | `h`, `?` | 列出所有可用命令 |
| `err` | | 显示最近捕获的错误信息 |
| `quit` | `q`, `exit` | 退出命令模式，回到透传 |

`generate 请求#内容` 跳过生成，直接把 `#` 后面的内容交给确认菜单；手写的内容
同样过一眼再发。

## 配置

`generate` 从 YAML 读 provider 配置，路径优先级：
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
