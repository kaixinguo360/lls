# 终端录制与回放

录制保存的是 PTY 的**原始字节与时间**，不是屏幕渲染结果；回放时再把字节喂给
屏幕模型。它服务三件事：复盘一次终端会话、按帧定位 agent 做过什么、导出成
asciicast 分享。

录制与 LLS 会话解耦：录制层有自己的 id 和索引，会话事件只记当前帧引用。

## 包边界

```text
src/lls/recording/
  __init__.py          公开 API（不 import replay）
  model.py             Frame / RecordingMeta / TerminalRef
  format.py            版本、通道、分段名、resize 载荷
  writer.py            TerminalRecorder
  reader.py            Recording
  ports.py             TerminalSink / TerminalCursor
  errors.py
  cli.py               ls / play / export
  __main__.py          python -m lls.recording
  replay/
    screen.py          ReplayScreen 协议 + 默认 pyte 实现
    player.py          Playback 状态机
    paint.py           ScreenPainter
    tui.py             全屏按键与时钟
    export.py          asciicast v2 导出
```

这个包不 import 任何 `lls.*`：核心读写只依赖标准库，`replay/` 额外只依赖
`pyte`。LLS 只在三处接进来：`app.py` 创建写端、`engine.py` 在 resize 时写
`r` 帧、`lls/cli.py` 挂 `recording` 子命令。回放需要的屏幕模型和行渲染器由
`src/lls/replay_adapter.py` 注入；把整个目录挪走时只需删掉这一个适配文件。

## 目录

```text
<recordings-dir>/<recording-id>/
  meta.json
  segment-000001.cast
  segment-000002.cast.gz
  index.jsonl
  tombstones.jsonl          （只在根目录，跨录制）
```

一个分段第一行是对象，之后每行是数组：

```json
{"version": 2, "id": "20261105T031000Z-1a2b3c4d", "segment": "segment-000002.cast",
 "created_at": 1793848664.9, "time_base": 812.437, "first_frame": 4501,
 "rows": 48, "columns": 160}
[0.0, "o", "aGVsbG8K"]
[0.013, "i", "bHMK"]
[0.020, "r", "MTYweDQ4"]
```

- `time_base` 是该段打开时距录制起点的秒数；段内 `t` 仍是相对时间，绝对时间
  为 `time_base + t`。分段计时不会在滚动后归零。
- `first_frame` 是该段第一帧的全局帧号；压缩、删旧段之后依然能继续编号。
- `rows`/`columns` 是该段开始时的尺寸，使每个分段都能自描述。
- 通道：
  - `o` 读子进程的字节；
  - `i` 写子进程的字节；
  - `r` 终端 resize，载荷为 `"{COLS}x{ROWS}"`（与 asciicast v2 同形）。

`meta.json`：

```json
{"version": 2, "id": "20261105T031000Z-1a2b3c4d", "created_at": 1793848664.9,
 "closed_at": 1793848700.1, "rows": 48, "columns": 160, "compressed": true,
 "frames": 8231, "duration": 12.345, "retained_from": 1,
 "segments": ["segment-000001.cast.gz"], "bytes": 4194304}
```

`rows`/`columns` 是**录制开始**时的尺寸；后续变化由 `r` 帧表达。

`index.jsonl` 每帧一行：

```json
{"frame": 4501, "segment": "segment-000002.cast", "time": 0.0,
 "at": 812.437, "offset": 148, "channel": "o", "bytes": 22}
```

`time` 是段内相对时间，`at` 是绝对时间。`frame` 单调递增；`terminal_cursor`
引用它就够了。

## 写端

`TerminalRecorder(root, rows=..., columns=..., compress=..., ...)`：

- 一次启动一个 recording id；目录 0700，文件 0600。
- 写 `i`/`o`/`r` 三类帧；大块字节按单次上限切帧。
- 达到 `max_recording_bytes` 时切新段并丢弃本次最早的分段，保留最新内容；
  `recording_id` 与 `frame` 不变，引用不会因滚动换 id。
- 关闭分段时按 `compress` 压缩；压缩用临时文件 + `os.replace`，崩溃只留下
  `.tmp` 或完整的 `.gz`。
- `fsync` 为真时每帧同步 stream 与 index。
- 关闭时写 `closed_at` 与 `duration`；崩溃留下的录制 `closed_at` 为 `null`。
- 总量超过 `max_total_bytes` 时按 `created_at` 删除整个旧录制，并在根目录
  `tombstones.jsonl` 留一条删除记录。

写失败（磁盘满、权限变化、压缩失败）只会停用录制本身；终端读写照常，原因由
`run_interactive` 打到 stderr。

## 读端

```python
from lls.recording import Recording

recording = Recording.open("~/.local/lls/recordings/<id>")
for frame in recording.frames():
    frame.number, frame.at, frame.channel, frame.data
```

- 分段从磁盘扫描，按数字排序；同名 `.cast` 与 `.cast.gz` 同时存在时优先
  `.gz`。
- 尾行损坏、gzip 中途断掉只影响当前分段；已读出的帧照常返回，原因进
  `recording.warnings`。
- `meta.json`/段头版本必须为 2；旧格式直接报错，不做迁移。
- `frame(n)` 在保留窗口外抛 `FrameUnavailable`；`frame_time(n)` 优先用
  `index.jsonl` 的 `at`。
- `geometry_at(t)` 从初始尺寸叠加 `r` 帧，供播放器确定画布。
- 整个目录不存在时区分 `RecordingNotFound` 与 `RecordingDeleted`（墓碑）。

## 回放

`Playback(recording, size=..., screen_factory=...)` 是纯状态机：

- `o` 喂屏幕模型；`r` 调 `resize`；`i` 与未知通道忽略。
- `seek(at)` 重置模型后快速重放到 `at`；终端状态是累积的，只能从头重建。
  本机实测 `PyteScreenModel` 约 0.6 MB/s，超长录制 seek 会慢，这是已知取舍。
- 默认屏幕是 `pyte` 薄封装；LLS 注入自己的 `PyteScreenModel`，回放与实时
  画面共用备用屏、宽度和容错规则。
- `ScreenPainter` 只画模型里的行、样式和光标；**不把录制字节直接写到宿主**，
  也不重放标题、剪贴板、鼠标上报。`i` 默认不显示：普通终端会把输入回显进
  `o`，重复显示；echo-off 时那里是明文密码。

全屏界面（`replay/tui.py`）：

```text
空格 暂停/继续     ←/→ 前后 5 秒     ↑/↓ 前后 1 秒
,/.  前后 0.1 秒   [/] 减速/加速     g/G 首尾
i    显示/隐藏输入  q/Esc/Ctrl-C 退出
```

窗口比录制尺寸小时裁切，不 reflow；底部状态栏显示 id、进度、倍速与输入
（仅在 `--show-input` 时）。

## 命令行

```bash
lls recording ls [--json]
lls recording play <id|path> [--speed N] [--frame N] [--time SEC]
                             [--size COLSxROWS] [--show-input] [--dump]
lls recording export <id|path> [-o FILE] [--with-input] [--size COLSxROWS]
```

- 所有录制命令都在 `lls recording` 下，二级子命令由 `recording/cli.py` 自己
  用 argparse 解析；顶层只在第一个位置参数处做一次分派。
- `export` 写 asciicast v2：首行 `width`/`height`/`timestamp`/`duration`，
  事件为 `[at, "o"|"i"|"r", text]`。`o` 用增量 UTF-8 解码，跨帧切断的多字节
  字符不会变成乱码；`i` 默认不导出。
- 非 tty 或没有 termios 时 `play` 提示改用 `--dump`/`export`；`--dump` 只输出
  末屏文本。
- 独立使用：`python -m lls.recording ls --root <dir>`；不经过 LLS 入口也
  不需要配置文件。

## 边界

- 只记录终端字节，不记录文件系统、任务或模型上下文。
- 录制不归属 session；删除 session 不删除录制。
- raw PTY 录制不包含 LLS 自己的命令行/agent 面板；要回放那层界面需要另记宿主
  渲染帧或屏幕快照。
- 录制默认关闭；`recording.enabled: true` 才创建写端。
- 引用是“事件发生时的录制位置”，不是内容区间。
- `retained_from` 表示当前仍保留的最小帧；更早引用已不可解析。
