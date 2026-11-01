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
| `user/message` | `{turn, content:[text], source, role:"user", id, lls?:{screen_seq}}` |
| `assistant/message` | `{turn, step, message:{role:"assistant", content:[parts], source, id}, usage?, lls?:{usage}}` |
| `tool/call` | `{turn, step, callId, name, arguments}` |
| `tool/result` | `{turn, step, message:{role:"tool", source:{kind:"tool",callId}, toolCallId, content:[text], isError, id}, meta}` |
| `lls/screen` | `{turn, content/blob, size?}` |
| `lls/compact` | `{summary, upto_seq}` |
| `lls/undo` | `{drop_from}` |
| `lls/model-switch` | `{model}` |

## parts

- `{"type":"text","text":...}`
- `{"type":"reasoning","text":...}`
- `{"type":"tool-call","id","name","arguments"}`

超过 8KB 的文本 part 落 `blobs/`，part 里留 `blob` 与 `size`，`text` 置空。

## 版本

- `meta.json.version = 2`；
- `session.version = 2`（LLS 自己的编号，不是 DSH 的 4）；
- 旧版本会话不读取、不迁移，列表里不出现。
