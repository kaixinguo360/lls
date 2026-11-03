# 终端录制与事件引用

终端录制保存**原始字节**，不保存屏幕渲染结果。它服务两个用途：

- 复现一次终端会话；
- 给离线训练提供 `(历史, 输入, 输出)` 样本。

录制与 LLS 会话解耦：录制层有自己的 ID 和索引，会话事件只存当前帧引用。

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

每个事件在 `lls.terminal_cursor` 记录事件发生时的录制位置：

```json
{"recording_id": "20261103T010203Z-7f3a", "frame": 1}
```

`frame` 是单调递增的录制帧号；`index.jsonl` 把它映射到时间、字节偏移和通道。
引用不是内容区间，也不保存正文、offset 或 hash。

## 事件覆盖

开启录制后，`Session.append` 在写入事件前读取 `TerminalRecorder.current_ref`，
所有事件都带同一个结构：`session`、`turn/*`、`step/*`、`system/message`、
`user/message`、`assistant/message`、`tool/call`、`tool/result`、`lls/*`。

## 配置

```yaml
paths:
  recordings_dir: ~/.local/lls/recordings

recording:
  enabled: false
  compress: true
  max_recording_bytes: 67108864
  max_total_bytes: 1073741824
  fsync: true
```

默认关闭。开启后由 `run_interactive` 创建 `TerminalRecorder`，引擎在每次
PTY 写入/读取时实时记录：

```text
写子进程 → i
读子进程 → o
```

这是真实子进程字节，不是屏幕重绘，也不是 bash 工具临时目录里的输出。
`paths.recordings_dir` 与 `paths.sessions_dir` 平级，默认是
`paths.root / "recordings"`；相对路径按配置文件所在目录解析。

`max_recording_bytes` 是**一次启动的录制目录**上限；目录内按大小写成
`segment-000001.cast` 等分段，超过上限时删除本次最早的分段，只保留最新
内容。`recording_id` 始终不变，`terminal_cursor` 不会因为滚动换 id。

`max_total_bytes` 是整个 `recordings_dir` 的上限；关闭或滚动时按
`created_at` 从旧到新删除整个旧录制目录，当前活动录制不删。
`compress: true` 在分段关闭时把 `.cast` 压成 `.cast.gz`。

`fsync: true` 在每帧写完后同步 `stream` 与 `index`；这是崩溃安全与吞吐的
取舍，默认开启。关闭压缩或 fsync 后性能更好，但断电/崩溃窗口更大。

## 边界

- 只记录终端字节，不记录文件系统、任务或模型上下文。
- 录制内容不进入模型请求。
- 录制默认关闭；只有 `recording.enabled: true` 才创建录制器。
- 每个 session 事件在 `lls.terminal_cursor` 记录当前帧引用：
  `{"recording_id": "<id>", "frame": <n>}`。
- 引用是“事件发生时的录制位置”，不是内容区间。
- 录制不归属 session；删除 session 不删除录制。
- 压缩、滚动、留存上限已实现；快照、反事实分支不在当前实现内。
- 崩溃恢复：启动时清掉 `.tmp`，校验并丢弃半份 gzip，保留完整明文或完整 gzip。
- 读取时会跳过损坏的尾行；不把半帧当成成功事件。
- `meta.retained_from` 表示当前录制仍保留的最小 frame；更早引用已不可解析。
