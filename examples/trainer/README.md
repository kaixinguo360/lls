# 参考训练器与评测器

主仓库只编排 `lls-learn`；这里给出能直接跑通 `cycle` 的最小实现。
它不是核心依赖，训练环境自己安装：

```bash
pip install torch transformers
```

## 文件

- `sft.py`：读 `sft.jsonl`，全参微调一个 causal LM，写到 `{adapter}`。
- `eval.py`：在 held-out 任务上算目标 token 的负对数似然，
  `score = 1 / (1 + loss)`。
- `make_demo.py`：造 v2 会话和 held-out 任务，只用于 smoke run。

## 数据契约

训练器读：

```text
{sft}       sft.jsonl，一行一条 {"messages": [...], "tools": [...], "meta": {...}}
{manifest}  manifest.json，含 samples 与 sha256
```

评测器读：

```text
{adapter}   训练器写出的模型目录
{tasks}     一行一条 {"messages": [{"role": "user", ...}], "target": "..."}
```

评测器写：

```json
{"score": 0.42, "loss": 1.38, "passed": 5, "total": 5}
```

`score` 必须在 `0..1`；`lls-learn` 只用它做门禁。

## 配置

```yaml
learning:
  train:
    command:
      - python
      - examples/trainer/sft.py
      - --sft
      - "{sft}"
      - --adapter
      - "{adapter}"
      - --model
      - Qwen/Qwen2-0.5B
      - --max-length
      - "96"
    timeout: 3600
  eval:
    command:
      - python
      - examples/trainer/eval.py
      - --adapter
      - "{adapter}"
      - --tasks
      - "{tasks}"
      - --metrics
      - "{metrics}"
    tasks: learning/tasks.jsonl
    timeout: 1200
```

命令的工作目录是启动 `lls-learn` 的目录；脚本路径按这个目录解析。占位符
都是绝对路径，训练器也可以只用 `LLS_*` 环境变量。

## 冒烟

```bash
python examples/trainer/make_demo.py \
  --sessions /tmp/lls-demo/sessions \
  --tasks /tmp/lls-demo/tasks.jsonl \
  --count 20

python -m lls.learn.cli \
  --config /tmp/lls-demo/config.yaml \
  --sessions /tmp/lls-demo/sessions cycle
```

CPU 全参微调 0.5B 很慢；生产用 GPU 或换 LoRA/QLoRA。这里的实现故意只依赖
`torch` + `transformers`，方便看清 SFT 的最小闭环。
