"""参考 SFT 训练器：读 sft.jsonl，全参微调一个小 causal LM。

这不是主仓库依赖；训练环境自己安装 transformers/torch。CPU 也能跑，
只是慢。示例：

    python examples/trainer/sft.py \
      --sft learning/runs/<id>/sft.jsonl \
      --adapter learning/runs/<id>/adapter \
      --model Qwen/Qwen2-0.5B \
      --epochs 1 --max-length 192
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


def _text(tokenizer, sample: dict) -> str:
    messages = sample.get("messages") or []
    try:
        return tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False
        )
    except Exception:
        rows = []
        for message in messages:
            content = message.get("content") or ""
            rows.append("{}: {}".format(message.get("role", "?"), content))
        return "\n".join(rows)


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
    parser.add_argument("--sft", default=os.environ.get("LLS_SFT"))
    parser.add_argument("--adapter", default=os.environ.get("LLS_ADAPTER"))
    parser.add_argument("--model", default="Qwen/Qwen2-0.5B")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--lr", type=float, default=5e-5)
    parser.add_argument("--max-length", type=int, default=192)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--local-files-only", action="store_true", default=True)
    parser.add_argument("--no-local-files-only", dest="local_files_only", action="store_false")
    args = parser.parse_args()

    if not args.sft or not args.adapter:
        raise SystemExit("--sft and --adapter are required")
    sft = Path(args.sft)
    adapter = Path(args.adapter)
    adapter.mkdir(parents=True, exist_ok=True)

    torch.set_num_threads(min(8, os.cpu_count() or 4))
    random.seed(0)
    torch.manual_seed(0)

    tokenizer = AutoTokenizer.from_pretrained(
        args.model, local_files_only=args.local_files_only
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        local_files_only=args.local_files_only,
        dtype=torch.float32,
    )
    model.config.use_cache = False
    model.gradient_checkpointing_enable()
    model.train()

    samples = _load(sft)
    if args.limit:
        samples = samples[: args.limit]
    if not samples:
        raise SystemExit("dataset is empty")
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    started = time.time()
    step = 0
    losses = []
    for epoch in range(max(1, args.epochs)):
        random.shuffle(samples)
        for sample in samples:
            encoded = tokenizer(
                _text(tokenizer, sample),
                return_tensors="pt",
                truncation=True,
                max_length=args.max_length,
                padding="max_length",
            )
            labels = encoded["input_ids"].clone()
            labels[encoded["attention_mask"] == 0] = -100
            output = model(**encoded, labels=labels)
            loss = output.loss
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            losses.append(float(loss))
            step += 1
            if step == 1 or step % 5 == 0:
                print(
                    "step {} epoch {} loss {:.4f} elapsed {:.1f}s".format(
                        step, epoch + 1, float(loss), time.time() - started
                    ),
                    flush=True,
                )
    model.save_pretrained(adapter, safe_serialization=True)
    tokenizer.save_pretrained(adapter)
    metrics = {
        "steps": step,
        "loss": sum(losses) / max(1, len(losses)),
        "duration": time.time() - started,
        "model": args.model,
        "samples": len(samples),
        "max_length": args.max_length,
    }
    (adapter / "training.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("saved {} ({:.1f}s)".format(adapter, metrics["duration"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
