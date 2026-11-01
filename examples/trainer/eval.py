"""参考评测器：在 held-out 任务上算目标 token 的负对数似然。

``score = 1 / (1 + loss)``，越大越好。它不判断具体任务是否完成，只回答
“模型是否更像训练数据里的目标回答”；真正的 verifier 可以替换这个脚本。

任务文件一行一条：

    {"messages": [{"role": "user", "content": "..."}], "target": "..."}
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


def _text(tokenizer, task: dict) -> tuple[str, str]:
    messages = task.get("messages")
    if isinstance(messages, list):
        prompt = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
    else:
        prompt = str(task.get("prompt", ""))
    target = str(task.get("target", ""))
    return prompt, target


def _load(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adapter", default=os.environ.get("LLS_ADAPTER"))
    parser.add_argument("--tasks", default=os.environ.get("LLS_TASKS"))
    parser.add_argument("--metrics", default=os.environ.get("LLS_METRICS"))
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--local-files-only", action="store_true", default=True)
    parser.add_argument("--no-local-files-only", dest="local_files_only", action="store_false")
    args = parser.parse_args()
    if not args.adapter or not args.tasks or not args.metrics:
        raise SystemExit("--adapter --tasks --metrics are required")

    torch.set_num_threads(min(8, os.cpu_count() or 4))
    tokenizer = AutoTokenizer.from_pretrained(
        args.adapter, local_files_only=args.local_files_only
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        args.adapter,
        local_files_only=args.local_files_only,
        dtype=torch.float32,
    )
    model.eval()

    rows = _load(Path(args.tasks))
    losses = []
    with torch.no_grad():
        for task in rows:
            prompt, target = _text(tokenizer, task)
            prompt_ids = tokenizer(prompt, return_tensors="pt")["input_ids"]
            target_ids = tokenizer(target, return_tensors="pt")["input_ids"]
            input_ids = torch.cat([prompt_ids, target_ids], dim=1)
            input_ids = input_ids[:, : args.max_length]
            labels = input_ids.clone()
            labels[:, : prompt_ids.shape[1]] = -100
            if input_ids.shape[1] <= prompt_ids.shape[1]:
                continue
            output = model(input_ids=input_ids, labels=labels)
            losses.append(float(output.loss))
    if not losses:
        raise SystemExit("no usable eval tasks")
    loss = sum(losses) / len(losses)
    score = 1.0 / (1.0 + loss)
    payload = {
        "score": score,
        "loss": loss,
        "passed": len(losses),
        "total": len(rows),
    }
    Path(args.metrics).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
