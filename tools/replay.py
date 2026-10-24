#!/usr/bin/env python3
"""语料回放与对拍 CLI（P1「相机」）。

用途：

* ``replay``   —— 把一段 .raw 字节流喂进屏幕模型，打印最终快照 JSON；
* ``compare``  —— 用不同的切块方式喂同一段语料，验证「分块不变性」；
* ``record``   —— 提示如何录制语料（POSIX 下用 script -q）。

这是开发期工具，直接 ``python tools/replay.py ...`` 运行（自动把 src 加入 sys.path）。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lls.term import PyteScreenModel  # noqa: E402
from lls.term.snapshot import capture, dumps  # noqa: E402


def _model(rows: int, columns: int) -> PyteScreenModel:
    return PyteScreenModel(rows=rows, columns=columns)


def _feed_chunked(model: PyteScreenModel, data: bytes, chunk: int) -> None:
    for i in range(0, len(data), chunk):
        model.feed(data[i : i + chunk])


def cmd_replay(args: argparse.Namespace) -> int:
    data = Path(args.corpus).read_bytes()
    model = _model(args.rows, args.columns)
    _feed_chunked(model, data, args.chunk)
    snap = capture(model, f"{args.corpus}@{len(data)}")
    print(dumps(snap))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """对同一语料用多种切块方式喂入，断言最终快照一致。

    默认切块集覆盖典型 os.read 边界：1、2、3、7、16、64、任意不整除长度。
    """
    data = Path(args.corpus).read_bytes()
    chunks = args.chunks if args.chunks else [1, 2, 3, 7, 16, 64, 4096]
    if not chunks or any(c < 1 for c in chunks):
        print("chunk sizes must be positive integers", file=sys.stderr)
        return 2

    reference: dict | None = None
    baseline = None
    for chunk in chunks:
        model = _model(args.rows, args.columns)
        _feed_chunked(model, data, chunk)
        snap = capture(model, f"chunk={chunk}")
        if baseline is None:
            baseline = snap
            reference = snap.to_dict()
            continue
        from lls.term.snapshot import diff

        d = diff(baseline, snap)
        if d:
            print(f"MISMATCH: chunk={chunk} differs from baseline")
            for line in d:
                print("  ", line)
            return 1
    assert reference is not None
    print("OK: all chunk sizes produce identical snapshot")
    print(json.dumps(reference, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    print(
        "在 POSIX 终端录制语料（把某个程序的完整输出落成 .raw）：\n"
        f"  script -q -c '{args.cmd}' {args.out}\n"
        "录制交互程序（vim/less/top）时注意其退出序列会被一并录下，"
        "这正是我们需要的完整字节流。\n"
        "Windows 下无法运行 script，请先在 WSL/Linux/macOS 录制后拷贝语料。"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="replay", description="LLS 终端语料回放工具")
    p.add_argument("--rows", type=int, default=24, help="可见屏幕行数")
    p.add_argument("--columns", type=int, default=80, help="可见屏幕列数")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("replay", help="喂语料并打印快照")
    r.add_argument("corpus")
    r.add_argument("--chunk", type=int, default=4096, help="每次 feed 的字节数")
    r.set_defaults(func=cmd_replay)

    c = sub.add_parser("compare", help="验证分块不变性")
    c.add_argument("corpus")
    c.add_argument("--chunks", type=int, nargs="*", help="切块集合（默认 1,2,3,7,16,64,4096）")
    c.set_defaults(func=cmd_compare)

    rec = sub.add_parser("record", help="打印录制语料的命令提示")
    rec.add_argument("--cmd", default="vim /tmp/x", help="要录制的命令")
    rec.add_argument("--out", default="tests/corpora/sample.raw", help="输出 .raw 路径")
    rec.set_defaults(func=cmd_record)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
