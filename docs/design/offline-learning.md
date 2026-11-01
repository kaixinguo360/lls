# 离线学习：从 v2 会话导出 SFT

本阶段只做**只读导出**：v2 会话 → SFT JSONL。训练器、评测和晋升留在
仓库外，主仓库不引入 torch/transformers，也不在正向推理进程里改权重。

## 取哪些轮次

- 只看 `meta.version = 2` 的会话；
- 只取 `turn/end` 里 `reason.kind = "completed"` 且 `lls.status = "ok"` 的轮次；
- 失败的轮次默认丢弃；`--include-failed` 只用于调试/分析，不建议直接训练。

## 怎么去掉教师

导出时按工具调用 id 删除：

- `ask_teacher` 的 `tool/call`；
- 对应的 `tool/result`；
- 只为求助而发的 assistant 消息。

学生自己后续写的反思/动作保留。这样样本里不会出现“向老师求助”的痕迹，
部署时没有教师也不会分布断裂。

## 样本格式

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

## 命令

```bash
# 默认读配置里的 paths.sessions_dir
lls-learn export --out learning/data/iter0

# 或指定目录
lls-learn --sessions ~/.local/lls/sessions export --out learning/data/iter0

# 只看能导出多少
lls-learn doctor
```

输出：

```text
learning/data/iter0/
  sft.jsonl
  manifest.json   # 样本数、会话数、include_failed、sha256
```

## 边界

- 只读：不调用 provider，不执行工具，不改会话；
- 不生成 DPO/RM 数据；先验证 SFT 是否有效，再考虑偏好学习；
- 不做训练、评测、adapter 晋升；这些由独立训练流程读取 `manifest.json`
  和 `sft.jsonl` 完成。
