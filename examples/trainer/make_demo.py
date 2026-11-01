"""造一组 v2 会话与 held-out 任务，用于离线流水线的 smoke run。

不是产品数据；只用来验证 export → train → eval → promote 真的能跑通。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lls.ai.sessions import SessionStore

SYSTEM = "你是 LLS 的终端助手。回答简洁，只给结论和一步建议。"
TRAIN = [
    ("把下面这句话改正式：这个功能有点问题。", "该功能存在一定问题。"),
    ("把下面这句话改正式：我马上看看。", "我将立即查看。"),
    ("把下面这句话改正式：这样不太行。", "该方案不可行。"),
    ("把下面这句话改正式：你帮我弄一下。", "请协助处理。"),
    ("把下面这句话改正式：这个报错又来了。", "该错误再次出现。"),
    ("把下面这句话改正式：先别动。", "请暂勿操作。"),
    ("把下面这句话改正式：我搞定了。", "已完成处理。"),
    ("把下面这句话改正式：还得再查查。", "仍需进一步排查。"),
    ("把下面这句话改正式：你确定吗？", "请确认。"),
    ("把下面这句话改正式：我这边没问题。", "本地验证通过。"),
]
EVAL = [
    ("把下面这句话改正式：这个不太稳。", "该方案稳定性不足。"),
    ("把下面这句话改正式：我待会儿处理。", "稍后处理。"),
    ("把下面这句话改正式：先这样吧。", "暂按此方案执行。"),
    ("把下面这句话改正式：好像可以了。", "当前已可用。"),
    ("把下面这句话改正式：你再看看。", "请再检查。"),
]


def _sessions(root: Path, count: int) -> None:
    store = SessionStore(root=root)
    turn = 0
    for session_index in range(max(1, count // 10)):
        session = store.create("stub", "m")
        session.append_system(SYSTEM, turn=turn + 1)
        for index in range(10):
            turn += 1
            prompt, target = TRAIN[(turn - 1) % len(TRAIN)]
            session.append_turn_start(turn)
            session.append_user(prompt, turn=turn)
            session.append_assistant(target, turn=turn, step=1)
            session.append_turn_end("ok", turn=turn)
        session.close()


def _tasks(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for prompt, target in EVAL:
            handle.write(
                json.dumps(
                    {
                        "messages": [{"role": "user", "content": prompt}],
                        "target": target,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", required=True)
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--count", type=int, default=60)
    args = parser.parse_args()
    _sessions(Path(args.sessions), args.count)
    _tasks(Path(args.tasks))
    print("sessions: {}".format(args.sessions))
    print("tasks:    {}".format(args.tasks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
