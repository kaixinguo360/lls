"""模型请求只从 AI 层发出（``lls.ai.stream_reply``）。

命令层自己 ``async for chunk in provider.chat(...)`` 是「等网络时界面不
响应」的老根因：那段循环没人读键盘，Ctrl-C 不生效、敲下的键也被吞掉。
把它钉成一条静态检查：新命令要流式请求，就得走 AI 层的那一份实现。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "lls"


def test_only_the_ai_layer_calls_provider_chat() -> None:
    offenders = []
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if relative.parts[0] == "ai":
            continue
        text = path.read_text(encoding="utf-8")
        if ".chat(" in text:
            offenders.append(str(relative))
    assert offenders == []
