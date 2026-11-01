# 离线后训练冒烟记录（2026-11-02）

目标不是证明模型质量，而是证明 `lls-learn` 的
export → train → eval → gate → promote 在真实模型、真实子进程上跑通。

## 环境

- Windows 10，Python 3.10.10
- torch 2.4.1+cpu（`torch.cuda.is_available() == False`）
- transformers 4.57.6
- 本地缓存模型：`Qwen/Qwen2-0.5B`
- CPU 全参微调，未使用 GPU

## 数据

- `examples/trainer/make_demo.py` 生成的合成 v2 会话
- 20 条训练样本，2 条会话
- 5 条 held-out 评测任务
- 数据只用于 smoke run，不代表真实任务分布

## 结果

| 项 | 值 |
|---|---|
| 基座评测 score / loss | 0.15894 / 5.29178 |
| 训练后 score / loss | 0.20517 / 3.87395 |
| 训练步数 | 20 |
| 训练耗时 | 610.52s |
| 评测耗时 | 46.25s |
| adapter | `model.safetensors` 约 1.98 GB |
| 门禁 | 通过，`current.json` 已更新 |
| run id | `20261102-034213-f4b725` |

## 实际命令

```bash
python examples/trainer/make_demo.py \
  --sessions learning-demo/sessions20 \
  --tasks learning-demo/tasks20.jsonl \
  --count 20

python -m lls.learn.cli \
  --config learning-demo/config.yaml \
  --sessions learning-demo/sessions20 cycle
```

配置里的训练命令：

```text
python examples/trainer/sft.py --sft {sft} --adapter {adapter}
  --model Qwen/Qwen2-0.5B --epochs 1 --max-length 96
```

评测命令：

```text
python examples/trainer/eval.py --adapter {adapter}
  --tasks {tasks} --metrics {metrics}
```

## 产物

```text
learning-demo/learning/runs/20261102-034213-f4b725/
  sft.jsonl
  manifest.json
  adapter/model.safetensors
  adapter/training.json
  train.json
  train.log
  eval.json
  eval.log
  promote.json
  cycle.json

learning-demo/learning/adapters/current.json
```

## 发现并修掉的问题

- 训练命令的工作目录原本被设成 run 目录，`examples/trainer/sft.py`
  这种相对路径会找不到。改为继承 `lls-learn` 的启动目录；占位符仍是绝对路径。
- `user/message` 之前没有 `turn`，导出按轮分组会得到 0 条样本。补上
  `turn` 后 export 正常。
- 评测器/训练器输出只在子进程结束后才写日志；本次单步约 30s，仍可完成。
  长训练建议训练器自己写增量日志。

## 结论

- 流水线可执行：真实模型训练、评测、门禁、晋升全部走通；
- 失败隔离有效：训练/评测失败不会改 `current.json`（有单元测试覆盖）；
- 本次是 CPU 小样本冒烟，**不是**质量提升的证明；生产训练应使用 GPU、
  更多真实会话和 held-out 任务。
