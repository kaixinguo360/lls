# 离线学习：导出、训练、评测与晋升

离线学习与正向推理彻底分离：`lls-learn` 只读写磁盘、起外部命令；主仓库
不引入 torch/transformers，也不在服务进程里改权重。训练器/评测器通过
数据集与 `metrics.json` 契约接入，可以是任意语言、任意环境。仓库里有一份
可直接跑通的最小实现：[examples/trainer](../../examples/trainer/README.md)。

## 一、数据

### 取哪些轮次

- 只看 `meta.version = 2` 的会话；
- 只取 `turn/end` 里 `reason.kind = "completed"` 且 `lls.status = "ok"` 的轮次；
- 失败轮次默认丢弃；`--include-failed` 只用于分析，不建议直接训练。

### 去掉教师

按工具调用 id 删除：

- `ask_teacher` 的 `tool/call`；
- 对应的 `tool/result`；
- 只为求助而发的 assistant 消息。

学生自己后续写的反思/动作保留。这样样本里不会出现“向老师求助”的痕迹，
部署时没有教师也不会分布断裂。

### 样本格式

`sft.jsonl` 一行一条：

```json
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": null,
     "tool_calls": [{"id": "c1", "type": "function",
                     "function": {"name": "bash", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "tools": [{"name": "bash", "description": "...", "parameters": {}}],
  "meta": {"session": "...", "turn": 1, "model": "stub/m", "created_at": 0}
}
```

`tools` 取该轮之前最近一条 `request/header` 里的工具定义；system 取最近
一条 `system/message`。

## 二、运行目录

一次训练/评测/晋升是一个 run：

```text
learning/
  runs/20261102-031500-ab12cd/
    run.json          # id、创建时间
    sft.jsonl
    manifest.json     # 样本数、会话数、sha256
    adapter/          # 训练器写出
    train.json        # 命令、退出码、耗时
    train.log
    eval.json         # score/passed/total + 命令、耗时
    eval.log
    promote.json      # 仅晋升成功时写
    cycle.json        # cycle 的最终状态
  adapters/
    current.json      # 当前晋升的 run/adapter/score
    history.jsonl     # 每次晋升追加一条
```

正向推理只读 `adapters/current.json`；训练失败、评测失败或门禁拒绝都不会
动这个指针。

## 三、配置

```yaml
paths:
  root: ~/.local/lls

learning:
  root: ~/.local/lls/learning
  train:
    command: ["python", "-m", "trainer.sft"]
    timeout: 3600
  eval:
    command: ["python", "-m", "trainer.eval"]
    tasks: learning/eval/tasks.jsonl
    timeout: 1800
  baseline: 0.55        # 没有 current 时的最低分
  min_delta: 0.01       # 必须比 current 高出这么多
```

命令里的占位符：

| 占位符 | 内容 |
|---|---|
| `{run}` / `{dataset}` | run 目录 |
| `{sft}` | `sft.jsonl` |
| `{manifest}` | `manifest.json` |
| `{adapter}` | `adapter/` |
| `{metrics}` | `eval.json` |
| `{tasks}` | 评测任务文件 |
| `{root}` / `{data}` / `{adapters}` | 学习根目录 / 数据目录 / adapter 目录 |

同时注入环境变量：`LLS_RUN`、`LLS_SFT`、`LLS_MANIFEST`、`LLS_ADAPTER`、
`LLS_METRICS`、`LLS_TASKS`。

## 四、命令

```bash
# 只导出，不训练
lls-learn export --out learning/data/iter0

# 单步
lls-learn doctor
lls-learn train --run learning/runs/20261102-031500-ab12cd
lls-learn eval --run learning/runs/20261102-031500-ab12cd
lls-learn promote --run learning/runs/20261102-031500-ab12cd

# 一次跑完：导出 → 训练 → 评测 → 晋升
lls-learn cycle
```

`cycle` 只在评测通过门禁时写 `adapters/current.json`；拒绝/失败时返回 1，
但已经产生的 run 目录、日志和指标都保留，便于排查和重跑。

## 五、契约

### 训练器

- 读 `{sft}` / `{manifest}`；
- 在 `{adapter}` 写出非空 adapter；
- 退出码 0 表示成功；非 0 即失败，不发布。

### 评测器

- 读 `{adapter}` 与 `{tasks}`；
- 在 `{metrics}` 写 JSON：

```json
{"score": 0.72, "passed": 18, "total": 25}
```

`score` 必须在 `0..1`；`passed`/`total` 可选，仅用于展示。评测器不写
`metrics`、写坏 JSON、score 越界都算失败。

### 门禁与晋升

- baseline = `max(learning.baseline, current.json.score)`；
- 通过条件：`score >= baseline + min_delta`；
- 通过后写 `adapters/current.json`（临时文件 + `os.replace`，不会半写）；
- 晋升历史追加到 `adapters/history.jsonl`。

## 六、边界

- `export` 只读会话：不调用 provider、不执行工具、不改会话；
- 训练/评测子进程是外部命令，主仓库不负责其依赖和 GPU；
- 不做在线 RL、DPO、奖励模型；先把 SFT 闭环跑通并量化；
- 任何一步失败都不改 `current.json`，线上模型保持原样。
