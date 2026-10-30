"""忙时等键盘：把引擎的中断语义翻成 AI 层的取消语义。

界面在等网络（模型答复、历史摘要、输入生成）时不该自己 ``read_chars``：
那样 Ctrl-C 没人管，敲下的键也会被丢掉。统一走 :func:`poll_keys` ——
它转到 :meth:`Services.wait`，并把引擎说的「用户中断 / 子进程退出」翻成
:class:`AgentCancelled` 的两个子类，由上层决定是继续这一轮还是退出会话。
"""

from __future__ import annotations

from lls.ai import AgentCancelled
from lls.ai.agent import STOP_POLL
from lls.session.errors import Interrupted, SessionEnded
from lls.session.services import Services

__all__ = ["Aborted", "SessionClosed", "poll_keys"]


class Aborted(AgentCancelled):
    """用户按 Ctrl-C 中止了正在等的这件事。会话继续。"""


class SessionClosed(AgentCancelled):
    """子进程退出，会话无法继续。"""


async def poll_keys(services: Services, timeout: float = STOP_POLL) -> None:
    """等 ``timeout`` 秒；Ctrl-C 抛 :class:`Aborted`，子进程退出抛
    :class:`SessionClosed`。

    可直接作为 :class:`~lls.ai.Agent` 的 ``poll``，也可以交给
    :func:`lls.ai.stream_reply`：等待期间按下的键不会丢，会在下一次读输入
    时按原顺序出现。
    """
    try:
        await services.wait(timeout)
    except Interrupted as e:
        raise Aborted(str(e)) from None
    except SessionEnded as e:
        raise SessionClosed(str(e)) from None
