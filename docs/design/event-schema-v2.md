# 会话事件 v2

`events.jsonl` 是会话的唯一事实来源。v2 只改记录形状，不引入训练语义：
不记录成功/失败、反馈正负、反思类型或 verdict。

## 信封

除 `session` 外，一行一个：

```json
{"type":"...","seq":1,"time":1793443458669,"data":{...}}
```

- `time` 是毫秒时间戳；
- `seq` 从 1 开始，单调递增；
- `session` 的字段在顶层，没有 `seq`/`time`；
- `surfaceOp` 只出现在 `user/message`、`tool/result`；
- `sourceEventSeqs` 只出现在 `tool/result`。

## 事件

| 事件 | `data` |
|---|---|
| `session` | `version`（LLS schema = 2）、`id`、`createdAt`、`cwd`、`isSeeded`、`delegationDepth`、`agentPreset`、`lls` |
| `turn/start` | `{turn}` |
| `turn/end` | `{turn, reason:{kind}, lls:{status,note?,usage?}}` |
| `step/start` / `step/end` | `{turn, step}` |
| `system/message` | `{turn, step, message:{role:"system", content:[text]}, lls:{render_id}}` |
| `request/header` | `{header:{config, adapterDefaults, tools}, lls:{render_id}}` |
| `request/context` | `{provider, model, contextWindow, lls:{budget, render_id}}` |
| `user/message` | `{content:[text], source, role:"user", id, lls?:{screen_seq}}` |
| `assistant/message` | `{turn, step, message:{role:"assistant", content:[parts], source, id}, usage?, lls?:{usage}}` |
| `tool/call` | `{turn, step, callId, name, arguments}` |
| `tool/result` | `{turn, step, message:{role:"tool", source:{kind:"tool",callId}, toolCallId, content:[text], isError, id}, meta:{raw?}}` |
| `lls/screen` | `{turn, content/blob, size?}` |
| `lls/compact` | `{summary, upto_seq}` |
| `lls/prune` | `{upto_seq, threshold, head, tail}` |
| `lls/undo` | `{drop_from}` |
| `lls/model-switch` | `{model}` |

## parts

- `{"type":"text","text":...}`
- `{"type":"reasoning","text":...}`
- `{"type":"tool-call","id","name","arguments"}`

超过 8KB 的文本 part 落 `blobs/`，part 里留 `blob` 与 `size`，`text` 置空。

## 工具结果的事实层

`tool/result` 的 `meta.raw` 是这次调用的结构化事实，**文本仍是唯一的重放来源**：

```json
"meta": {"raw": {"tool": "bash", "cwd": "/repo", "exit": 2, "elapsed": 37.4, "timed_out": false}}
```

- 只放**从文本里不好可靠取回来**的字段：退出码、耗时、光标、行范围、替换处数。
  正文（屏幕、stdout、文件内容）不进 `raw` —— 它已经在 `content` 里，再存一份就是双倍体积。
- `raw.tool` 是派生值，`tool/call.name` 才是权威；两者由同一次调用产出。
- 旧事件没有 `meta.raw` 是合法的：它只对分析、统计、换一种渲染有用，缺了就退回文本。
- 文本与 `raw` 由同一次工具返回（`Observation`）产出，所以不会各说各话；改格式时
  两者一起改。

## 终端引用

开启录制时，每条事件在 `lls.terminal_cursor` 记录事件发生时的
`{"recording_id": "...", "frame": N}`；录制停用后不再带这个字段。帧到时间的
映射由录制包的 `index.jsonl` 负责，事件本身不存区间或正文。详见
[终端录制与回放](terminal-recording.md)。

## 版本

- `meta.json.version = 2`；
- `session.version = 2`（LLS 自己的编号，不是 DSH 的 4）；
- 旧版本会话不读取、不迁移，列表里不出现。
