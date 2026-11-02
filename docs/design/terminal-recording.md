# 终端录制与事件引用

终端录制保存**原始字节**，不保存屏幕渲染结果。它服务两个用途：

- 复现一次终端会话；
- 给离线训练提供 `(历史, 输入, 输出)` 样本。

录制与 LLS 会话解耦：录制层有自己的 ID 和索引，会话事件只存一个引用字符串。

## 目录

```text
<recording-root>/<recording-id>/
  meta.json      录制元数据
  stream.cast    JSONL：时间、通道、base64 字节
  index.jsonl    frame → 时间/字节偏移
```

`stream.cast` 第一行是对象，后续每行是数组：

```json
{"version": 1, "id": "20261103T010203Z-7f3a", "created_at": 1793...}
[0.0, "i", "bHMK"]
[0.12, "o", "YS50eHQK"]
```

通道只有：

```text
i  输入
o  输出
```

索引每行：

```json
{"frame": 1, "time": 0.0, "offset": 0, "channel": "i", "bytes": 4}
```

## 引用

会话事件只存一个字符串：

```json
"meta": {"terminal_ref": "20261103T010203Z-7f3a:1"}
```

解析规则是最后一个冒号之前为 recording id，之后为 frame。  
引用不存 offset、hash、起止区间；这些由录制层维护。

## Shell 集成

`Shell` 可选接收 `TerminalRecorder`。开启后：

- 执行前记录一条 `i`；
- 结束后记录 stdout、stderr 的原始字节为 `o`；
- 工具结果通过 `ToolResult.meta["terminal_ref"]` 返回最后一个输出帧；
- 工具正文仍按原有预算裁剪，录制内容不受裁剪影响。

`Agent` 把 `ToolResult.meta` 写入 `Message.meta`，会话层落到
`tool/result.data.meta`；历史回放时再带回 `Message.meta`。

## 边界

- 只记录终端字节，不记录文件系统、任务或模型上下文。
- 录制内容不进入模型请求。
- 录制默认关闭；调用方显式传入 `TerminalRecorder` 才启用。
- 压缩、快照、反事实分支不在当前实现内。
