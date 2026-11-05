"""一轮任务：快照 → 拼上下文 → 压缩 → Agent 循环 → 结果。

这里是 ``agent`` 命令里不属于界面的那一半：读终端快照、按预算裁历史、
上下文溢出时压缩重试、把每条消息写进事件流。状态行、输入行这些界面行为
都留在命令层，通过 ``on_event`` / ``on_phase`` 回调接进来 —— 本模块不
认识终端界面，也不认识斜杠命令。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Tuple

from lls.ai.agent import (
    Agent,
    AgentCancelled,
    AgentEvent,
    AgentEventKind,
    stream_reply,
)
from lls.ai.sessions import (
    COMPACT_TARGET_RATIO,
    COMPACT_TRIGGER_RATIO,
    estimate_tokens,
    fit,
    task_message,
)
from lls.ai.sessions.events import SUMMARY_PREFIX
from lls.ai.types import Message, ToolCall
from lls.offload import offload

__all__ = [
    "CompactFailed",
    "CompactMode",
    "Turn",
    "TurnResult",
    "TurnState",
]

# 摘要是一次网络请求：输入按「这次要丢掉多少」伸缩 —— 丢得多就多读原文，
# 否则早期任务目标会悄悄漏掉；丢得少就别喂一个大请求，那既浪费，也容易被
# 摘要请求自己的窗口卡住。超过上限就分块滚动。
COMPACT_SOURCE_CHARS_MIN = 20_000    # 输入下限（字符）：短历史够用
COMPACT_SOURCE_CHARS_MAX = 40_000    # 单个请求的上限（字符）：再长就分块滚动
COMPACT_SOURCE_CHARS_PER_TOKEN = 0.5  # 每丢掉一个 token 带多少字原文
COMPACT_SOURCE_HEAD_RATIO = 0.2     # 其中留给最早原文的比例：任务锚点
SUMMARY_TOKENS_RATIO = 0.15         # 摘要目标 ≈ 被丢 token 的 15%
SUMMARY_TOKENS_MIN = 600            # ≈ 600 字：够写下几个路径、几条命令
SUMMARY_TOKENS_MAX = 4000
SUMMARY_CHARS_PER_TOKEN = 1.0       # 中文 1 字 ≈ 1 tok
COMPACT_MAX_ROUNDS = 4              # 超长历史分块滚动压缩的轮数上限
COMPACT_PROMPT = """\
把下面这段对话压缩成要点，供另一个模型接着做同一件事时使用。

按下面的结构输出，每一节都要有；没有内容就写「（无）」。用短句，不要展开成段落。

## 目标与原话
- 用户最初要什么、后来改成了什么；措辞有影响的地方照抄原话

## 环境与事实
- 路径、命令、分支、端口、版本，以及已经确认过的观察结果

## 做过的决定
- 决定了什么，为什么

## 失败与修正
- 报错原文、怎么错的、最后怎么绕过或修好

## 终端现状
- 屏幕上最后的关键几行，服务与进程现在是什么状态

## 未完成
- 明确提出但还没做完的

## 下一步
- 紧接着该做的那个动作

规则：
- 保留精确的路径、命令、错误串、标识符与数字，别改写成概括
- 给了「上一版摘要」时以它为底稿合并：仍然成立的留着，被推翻或已经做完的
  删掉，不要另起一份并列的清单，也不要照抄一遍
- 不要提到这次摘要请求，也不要评价这段对话

用中文，整段不超过 {chars} 字。"""
#: 分块压缩时补在正文前的一句：否则模型会把这一块当成被压历史的全部。
COMPACT_PART_NOTE = "这是较长历史中的一段，其余部分会逐块并入，不是完整历史。\n\n"
TASK_LINES = 80           # 每轮任务消息里带多少行终端画面
# 自动压缩时，丢掉的上下文少于这个量就不值得压：摘要请求本身也要花 token，
# 压掉的还没摘要贵，不如直接截断。用户显式 /compact 不受这一条限制 —— 那是
# 明确的指令，只受「压了必须能前进」这一条实质约束。
MIN_COMPACT_TOKENS = 1024
# 修剪：不发请求，只把超长的工具输出按预算剪成「开头 + 省略标记 + 结尾」。
# 压缩之前先走这一步 —— 剪完往往就回到水位以内，那次摘要请求根本不必发。
# 原文还在磁盘上，模型要细节可以再读一次；修剪只改「这次看到多少」。
PRUNE_THRESHOLD_CHARS = 8_192   # 超过这么多字才剪
PRUNE_HEAD_CHARS = 3_072        # 开头留多少字：命令、报错、路径都在前面
PRUNE_TAIL_CHARS = 1_024        # 结尾留多少字：最后一行往往正是要接着看的
# 被摘要换掉的那段，至少要缩到一半以下才算划算：压完几乎没变小，就等于
# 花一次请求买了同一段历史，还丢了原文。
COMPACT_SHRINK_RATIO = 0.5
# 「上下文装不下」的报错长什么样。各家措辞不同，只认能确定的那几种：
# 认错会把限流、鉴权之类的错误也说成窗口问题，白白丢掉一段历史。
_OVERFLOW_HINTS = (
    "context length",
    "context_length",
    "context window",
    "context_window",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "input is too long",
    "reduce the length",
    "exceeds the maximum",
    "上下文长度",
    "上下文超过",
    "超过最大",
)


class CompactMode(str, Enum):
    """压缩的三种来路 —— 决定压多深、要不要先修剪。

    自动压缩要留出水位以上的余量，所以只压回目标线；手动和溢出都压到底，
    区别在修剪：手动是用户明确要摘要，先剪一刀会让「已压缩」名不副实；
    溢出是请求刚被拒过，先剪（不发请求）往往就能救回来。
    """

    AUTO = "auto"
    OVERFLOW = "overflow"
    MANUAL = "manual"


class CompactFailed(Exception):
    """摘要请求没成功：这一次没压成，历史原样保留。"""


class TurnState:
    """跨轮保留、但只属于任务执行的状态。

    ``render_id`` 让同一份请求信封只写一次；``transcript`` 是教师工具每次
    请求时读的当前上下文；``window`` 留着最近一次裁出来的窗口，供状态行
    显示用量（命令层只读，不复制）。
    """

    def __init__(self) -> None:
        self.render_id = ""
        self.transcript: list[Message] = []
        self.window: Any = None


@dataclass(frozen=True)
class TurnResult:
    """一轮任务的结果：命令层据此收尾、显示与决定要不要往上抛。

    ``status`` 是 ``ok`` / ``aborted`` / ``error``；``text`` 是最终答复
    （只有 ``ok`` 有）；``note`` 是要写进 ``turn/end`` 的那句话；
    ``turn_note`` 是这一轮要不要在状态行留的备注（裁过、压过、出错）；
    ``stopped`` 是 Agent 循环里抛出的取消异常，命令层据此区分「中断」
    与「会话结束」。
    """

    status: str
    label: str = ""
    text: str = ""
    note: str = ""
    turn_note: str = ""
    usage: Any = None
    window: Any = None
    stopped: BaseException | None = None


class Turn:
    """一轮任务的主体：取快照、裁历史、压缩、跑 Agent、写事件。

    一次 :meth:`run` 就完成一轮；跨轮的请求信封指纹、教师转录与最近窗口
    放在共享的 :class:`TurnState` 里。界面行为一律走回调：
    ``on_event`` 收 Agent 的进度，``on_phase`` 收 ``compacting`` /
    ``awaiting`` 这两个相。
    """

    def __init__(
        self,
        *,
        services: Any,
        view: Any,
        provider: Any,
        model: str,
        session: Any,
        tools: Any,
        budget: int = 0,
        provider_id: str = "",
        catalog: Any = None,
        system: str = "",
        state: TurnState | None = None,
        turn: int = 0,
        poll: Callable[[float], Any] | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        on_phase: Callable[[str], None] | None = None,
    ) -> None:
        self._services = services
        self._view = view
        self._provider = provider
        self._model = model
        self._session = session
        self._tools = tools
        self._budget = max(0, int(budget))
        self._provider_id = provider_id
        self._catalog = catalog
        self._system = system
        self._state = state if state is not None else TurnState()
        self._turn_no = int(turn)
        self._poll = poll
        self._on_event = on_event
        self._on_phase = on_phase
        self._step = 0
        self._reasoning = ""
        self._calls: dict[str, str] = {}
        self._call_seqs: dict[str, int] = {}
        self._compacted_now = False
        self._pruned_now = False
        self._compact_error = ""
        self._compact_skip = ""

    async def run(self, task: Any) -> TurnResult:
        """一轮任务的主体。覆盖层的生死由 :meth:`_one` 负责。"""
        self._start_turn()
        label = "" if task is None else task
        if task is None:
            # 接着往下执行：窗口就是现有会话，什么都不新加。
            message = None
            pending: List[Any] = []
        else:
            screen = self._view.snapshot(TASK_LINES)
            self._view.mark(screen)
            message = task_message(task, screen)
            pending = [Message("user", message)]
        # 裁历史要把整条会话读出来算 token：几十毫秒到几百毫秒，放线程里，
        # 免得这段 CPU 把 Ctrl-C 一起堵住。
        window = await offload(self._prepare_window, pending)
        self._phase("compacting")
        try:
            if await self._compact(window, pending):
                window = await offload(self._prepare_window, pending)
        except CompactFailed as e:
            # 压缩没做成不影响这一轮：退回硬截断，照常请求，事后再说明。
            self._compact_error = str(e)
        if message is not None:
            self._submit(task, screen)
        self._phase("awaiting")

        async def before_step(history: List[Message]) -> List[Message]:
            """每次请求之前再量一次：一轮几十步，工具结果只增不减。

            轮首压过一次，管不到轮内 —— 那一段才是真正把窗口撑爆的地方。
            没到水位就原样返回 ``fit`` 裁过的那份（按步裁，不会拆开
            tool_calls），到了水位就地压一次。
            """
            if self._session is None or self._budget <= 0:
                return history
            window = await offload(self._prepare_window, ())
            if not window.dropped and window.used < int(
                window.budget * COMPACT_TRIGGER_RATIO
            ):
                return window.messages
            if self._compact_error:
                # 这一轮已经压失败过一次：再试只会把同一笔等待与同一个错误
                # 重复到每一步。硬截断先顶上，下一轮重新判断。
                return window.messages
            self._phase("compacting")
            try:
                if await self._compact(window, ()):
                    window = await offload(self._prepare_window, ())
            except CompactFailed as e:
                self._compact_error = str(e)
            finally:
                self._phase("awaiting")
            return window.messages

        self._state.transcript = list(window.messages)
        self._record_request(self._system)
        result: Any = None
        agent: Any = None
        for attempt in (0, 1):
            agent = Agent(
                self._provider,
                self._model,
                system=self._system,
                tools=self._tools,
                on_event=self._on_agent_event,
                poll=self._poll,
                on_message=self._on_message,
                before_request=before_step,
            )
            try:
                # 第二次跑的是同一段会话：任务那条已经在历史里了（见 _recover）。
                result = await agent.run(message if attempt == 0 else None, window.messages)
                break
            except AgentCancelled as e:
                # 取消只有两种来路：用户中断（这一轮放弃）与子进程退出
                # （会话结束）。命令层按 ``stopped`` 的类型决定收尾后的动作。
                self._keep_partial(agent)
                return self._result("aborted", label, note=str(e), stopped=e, window=window)
            except Exception as e:  # noqa: BLE001
                # 装不下是唯一值得再花一次请求的情形：压到最小再来一遍。
                # 别的错误重试只会把同一条报错再写一遍。
                if attempt == 0 and _context_overflow(e):
                    window = await self._recover(window)
                    continue
                note = "错误：{}".format(e)
                return self._result("error", label, note=note, window=window)

        summary = result.summary if result.summary.strip() else "（无文本答复）"
        return self._result(
            "ok", label, text=summary, usage=result.usage, window=window
        )

    async def compact_now(self) -> Tuple[bool, Any, str]:
        """立刻压一次历史（``/compact``）：压到底，不等水位。

        返回 ``(有没有压, 压完的窗口, 没压成的原因)`` —— 最后一项只在没压时
        有意义。记账与摘要请求都在这里，命令层只负责说给用户听。
        """
        window = await offload(self._prepare_window, ())
        compressed = await self._compact(window, (), mode=CompactMode.MANUAL)
        after = await offload(self._prepare_window, ())
        return compressed, after, self._skip_reason()

    # ---- 记录 ----

    def _start_turn(self) -> None:
        """开一轮：清掉上一轮残留的进度，写下 ``turn/start``。"""
        self._step = 0
        self._reasoning = ""
        self._calls.clear()
        self._call_seqs.clear()
        self._compacted_now = False
        self._pruned_now = False
        self._compact_error = ""
        self._compact_skip = ""
        if self._session is not None:
            self._session.append_turn_start(self._turn_no)

    def _submit(self, task: str, screen: str) -> None:
        """把这一轮发出去的东西写进会话：先画面，再任务。

        先写画面，后写任务：任务那条是这一轮开始的地方，它指向画面；画面
        写不进去就只记任务，重放时这一轮少一段输入，仍是同一条消息。
        """
        if self._session is None:
            return
        snapshot = self._session.append_screen(screen, self._turn_no)
        self._session.append_user(
            task, screen=snapshot.seq if snapshot is not None else 0, source="typed"
        )

    def _on_agent_event(self, event: AgentEvent) -> None:
        """把 Agent 的进度交给界面，同时维护本轮的步号与推理缓冲。"""
        if self._on_event is not None:
            self._on_event(event)
        if event.kind is AgentEventKind.STEP:
            self._step = event.step
            self._reasoning = ""
            if self._session is not None:
                self._session.append_step_start(self._turn_no, self._step)
        elif event.kind is AgentEventKind.REASONING:
            self._reasoning += event.text
        elif event.kind is AgentEventKind.STEP_END:
            if self._session is not None:
                self._session.append_step_end(self._turn_no, event.step)

    def _record_request(self, system: str) -> None:
        """把这一次请求的信封记下来，内容没变就不重复写。"""
        if self._session is None:
            return
        tools: List[Dict[str, Any]] = []
        for entry in self._tools:
            payload = entry.tool.to_payload().get("function")
            if isinstance(payload, dict):
                tools.append(payload)
        params: Dict[str, Any] = {}
        context_window = 0
        if self._catalog is not None and self._provider_id:
            try:
                provider = self._catalog.provider_config(self._provider_id)
            except Exception:  # noqa: BLE001 - 配置错误不该挡住记录
                provider = None
            if provider is not None:
                params = dict(getattr(provider, "params", {}) or {})
                context_window = int(getattr(provider, "context_window", 0) or 0)
        config = {"provider": self._provider_id, "model": self._model}
        config.update(params)
        header = {"config": config, "adapterDefaults": {}, "tools": tools}
        fingerprint = json.dumps(
            {
                "system": system,
                "header": header,
                "budget": self._budget,
                "context_window": context_window,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        render_id = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()
        if render_id == self._state.render_id:
            return
        self._state.render_id = render_id
        self._session.append_system(system, self._turn_no, 1, render_id)
        self._session.append_request_header(header, render_id)
        self._session.append_request_context(
            self._provider_id,
            self._model,
            context_window,
            self._budget,
            render_id,
        )

    def _keep_partial(self, agent: Any) -> None:
        """把中断前已经吐出来的正文补进会话。

        屏幕上已经显示的答复，上下文里也该有：不然下一轮只能从头再来一遍。
        """
        text = agent.partial_text
        if self._session is not None and text:
            self._session.append_assistant(
                text,
                reasoning=self._reasoning,
                model=self._model,
                turn=self._turn_no,
                step=self._step,
            )

    def _prepare_window(self, pending: Any) -> Any:
        """本轮要带上的上下文：按预算从事件里裁出来。

        当前任务作为 ``pending`` 一起算预算：它还没进历史，但一定会随请求
        发出去，不把它算进去就会「裁完仍然超窗」。
        """
        messages = self._session.history() if self._session is not None else []
        window = fit(messages, self._budget, pending)
        self._state.window = window
        return window

    async def _compact(
        self,
        window: Any,
        pending: Any = (),
        mode: CompactMode = CompactMode.AUTO,
    ) -> bool:
        """把旧历史压成摘要，一次压到位；压不成就不动历史。

        三种来路（见 :class:`CompactMode`），只差两件事：**压多深**、**剪不剪**。

        * 自动：到水位才动手，先修剪再摘要 —— 修剪不发请求，剪完常常就回到
          水位以内，那一次摘要根本不必发。压到 ``COMPACT_TARGET_RATIO`` 的
          水位，好让后面几轮有地方攒。
        * 溢出：请求刚被「装不下」顶回来。先剪（同样的毛病通常就是几条巨大
          的输出），再压到底 —— 只留最后一块，其余全进摘要。
        * 手动：用户按了 ``/compact``。不修剪、不问水位、不看
          :data:`MIN_COMPACT_TOKENS`，直接压到底 —— 用户要的是一次到底，
          而不是压回水位再按一次。

        三种都守同一条实质约束：**压了必须能前进**（cutoff 越过
        :meth:`compacted_upto`，顶部摘要不自压），**摘要必须真的变小**
        （不到被丢量的一半就放弃，宁可硬截断，也不花一次请求买回同一段历史）。

        ``target.keep_from`` 是消息列表里第一个被保留的下标，据此换算成「裁到
        哪一条事件」。摘要本身顶在头部，不能拿消息条数相减来算，否则每次都少
        裁一条，第二次以后 cutoff 不前进，压缩就只剩第一次生效。

        只有手动那条路才会向用户交代结果，所以也只有它记原因
        （``self._compact_skip``）：自动压缩的每次跳过都只是一次静默的否决，
        没人会问「为什么没压」。
        """
        self._compact_skip = ""
        if self._session is None:
            return False
        if window.budget <= 0:
            self._compact_skip = "no-budget"
            return False
        deep = mode is not CompactMode.AUTO
        waterline = int(window.budget * COMPACT_TRIGGER_RATIO)
        if not deep and not window.dropped and window.used < waterline:
            return False

        # 压到底只留最后一块（那是当前这一步，压不掉）；自动压缩压回目标水位。
        target_line = 1 if deep else int(window.budget * COMPACT_TARGET_RATIO)
        messages, marks = self._session.history_marks()
        # 顶部那条摘要不参与压缩：它既不该算进被压掉的区间，也不能被自己覆盖。
        pinned = 1 if messages and _is_summary(messages[0]) else 0
        target = fit(messages, target_line, pending)
        cut = target.keep_from

        if mode is not CompactMode.MANUAL and self._prune(marks):
            window = self._prepare_window(pending)
            if not window.dropped and window.used < waterline:
                self._pruned_now = True
                return True
            messages, marks = self._session.history_marks()
            pinned = 1 if messages and _is_summary(messages[0]) else 0
            target = fit(messages, target_line, pending)
            cut = target.keep_from

        if cut <= pinned or cut > len(marks):
            self._compact_skip = "no-progress"
            return False
        if not deep and target.dropped_tokens < MIN_COMPACT_TOKENS:
            return False
        source, chars = _compact_limits(target.dropped_tokens, window.budget)
        # 摘要自己也要占位置：按它大概多长把目标线再往下压一点，否则压完刚好
        # 贴着目标线，加上摘要就又超了 —— 下一次请求立刻又回到水位。
        if not deep and target_line > chars:
            deeper = fit(messages, target_line - chars, pending)
            if cut < deeper.keep_from <= len(marks):
                target = deeper
                cut = deeper.keep_from
        cutoff = marks[cut - 1]
        if cutoff <= 0 or cutoff <= self._session.compacted_upto():
            self._compact_skip = "no-progress"
            return False
        previous = messages[0].content[len(SUMMARY_PREFIX):] if pinned else ""
        summary = await self._summarize(messages[pinned:cut], previous, source, chars)
        if estimate_tokens(summary) > target.dropped_tokens * COMPACT_SHRINK_RATIO:
            self._compact_skip = "no-shrink"
            return False
        self._session.append_compact(summary, cutoff)
        self._compacted_now = True
        return True

    def _prune(self, marks: Any) -> bool:
        """把历史里超长的工具输出剪一刀；记下这一刀就返回 True。

        走到这里说明已经到水位（或请求刚被拒），而这时候最占地方的通常正是
        几条巨大的画面转储。原文都完整地躺在磁盘上，模型要细节可以再读一次；
        留着全文把窗口撑爆，才是真的没法继续。最新那一条也照剪 —— 撑爆窗口
        的往往就是它。

        手动压缩（``/compact``）不走这一步：那一次本来就要把整段换成摘要，
        多剪一刀既省不下请求，也让「已压缩」和实际做的不是一回事。
        """
        if self._session is None or not marks:
            return False
        upto = marks[-1]
        floor = self._session.pruned_upto()
        if upto <= floor or not self._session.oversized_results(
            floor, upto, PRUNE_THRESHOLD_CHARS
        ):
            return False
        return (
            self._session.append_prune(
                upto, PRUNE_THRESHOLD_CHARS, PRUNE_HEAD_CHARS, PRUNE_TAIL_CHARS
            )
            is not None
        )

    async def _recover(self, window: Any) -> Any:
        """上下文溢出之后：压到底，再试一次。

        请求已经被拒过，水位和目标都不再重要：先剪掉超长的工具输出，再只留
        最后一块、其余全进摘要。压不成也照样返回 —— 再试一次才知道是真装不
        下，还是这次能过。
        """
        self._phase("compacting")
        try:
            await self._compact(window, (), mode=CompactMode.OVERFLOW)
        except CompactFailed as e:
            self._compact_error = str(e)
        finally:
            self._phase("awaiting")
        return await offload(self._prepare_window, ())

    def _skip_reason(self) -> str:
        """这次 /compact 没压成，给一句实话：是没得可压，还是不知道压到哪。"""
        if self._compact_skip == "no-budget":
            return "无需压缩：未知该模型的上下文窗口大小"
        if self._compact_skip == "no-progress":
            return "无需压缩：上次压缩点之后没有新的历史"
        if self._compact_skip == "no-shrink":
            return "无需压缩：摘要并没有比原文短多少"
        return "无需压缩：没有可压缩的历史"

    async def _summarize(
        self,
        messages: Any,
        previous: str = "",
        source: int = COMPACT_SOURCE_CHARS_MIN,
        chars: int = int(SUMMARY_TOKENS_MIN * SUMMARY_CHARS_PER_TOKEN),
    ) -> str:
        """让模型把要丢掉的那段压成要点。

        ``previous`` 是上一版摘要，有它就当底稿合并出新的一版。不合并的话，
        每次都要从原文重摘整段前缀，既贵，早先的结论也会在一次次复述里磨掉。

        ``source`` 是这一批消息最多带多少字原文、``chars`` 是摘要写多长，都由
        :func:`_compact_limits` 按被丢量算出来。正文超过 ``source`` 就分块，
        每块压出的摘要当下一块的底稿：只留尾巴的旧做法会切掉最早的任务目标，
        而十万 token 的原文也塞不进一次请求。

        所有块都成功才返回；任何一块失败都抛 :class:`CompactFailed` —— 不
        返回空串，那样 /compact 会把一次网络错误说成「无需压缩」，真正的原因
        就没人知道了。事件流因此一条也不写，历史不动。Ctrl-C 照常抛出。
        """
        blocks = _blocks(messages, source)
        if not blocks:
            raise CompactFailed("没有可摘要的内容")
        summary = previous
        for block in blocks[:COMPACT_MAX_ROUNDS]:
            text = _transcript(block, source)
            if not text.strip():
                continue
            # 分块时要说明「这不是全部」：否则模型会以为被压历史到此为止。
            body = COMPACT_PART_NOTE if len(blocks) > 1 else ""
            if summary:
                body += "上一版摘要：\n" + summary + "\n\n新增的对话：\n" + text
            else:
                body += text
            summary = await self._ask_summary(body, chars)
        hidden = len(blocks) - COMPACT_MAX_ROUNDS
        if hidden > 0:
            summary += "\n（更早的 {} 块未展开，已省略）".format(hidden)
        # 空摘要写进事件流，重放时就只剩一个光秃秃的前缀：宁可报失败。
        if not summary.strip():
            raise CompactFailed("摘要请求没有返回内容")
        return summary.strip()

    async def _ask_summary(self, body: str, chars: int) -> str:
        """发一次摘要请求。可中断，失败一律算 ``CompactFailed``。"""
        request = [
            Message("system", COMPACT_PROMPT.format(chars=chars)),
            Message("user", body),
        ]
        try:
            text, _calls, _usage = await stream_reply(
                self._provider,
                self._model,
                request,
                poll=self._poll,
            )
        except AgentCancelled:
            raise
        except Exception as e:  # noqa: BLE001 - 网络、配置错误都要说出来
            raise CompactFailed("摘要请求失败（{}）".format(e)) from e
        return text.strip()

    def _on_message(self, message: Any, usage: Any) -> None:
        """Agent 的每条消息 → 一条事件；user 由本命令自己记，跳过。"""
        if self._session is None:
            return
        self._state.transcript.append(message)
        if message.role == "assistant":
            self._session.append_assistant(
                message.content or "",
                message.tool_calls,
                reasoning=self._reasoning,
                model=self._model,
                usage=usage,
                turn=self._turn_no,
                step=self._step,
            )
            for call in message.tool_calls:
                self._calls[call.id] = call.name
                event = self._session.append_tool_call(
                    call, self._turn_no, self._step
                )
                if event is not None:
                    self._call_seqs[call.id] = event.seq
        elif message.role == "tool":
            text = message.content or ""
            call_id = message.tool_call_id or ""
            status = "error" if text.startswith("error:") else "ok"
            name = self._calls.get(call_id, "")
            # 事实层：文本照旧（重放用它），另存一份"从文本里不好可靠取回来"的
            # 字段。名字以 tool/call 为准，这里补进去让每条结果自描述。
            raw = dict(getattr(message, "meta", None) or {})
            if raw:
                raw.setdefault("tool", name)
            self._session.append_tool_result(
                ToolCall(call_id, name, ""),
                text,
                status,
                self._turn_no,
                self._step,
                self._call_seqs.get(call_id, 0),
                meta={"raw": raw} if raw else None,
            )

    def _turn_note(self, window: Any) -> str:
        """这一轮要不要在覆盖层留一句话：裁过、压过、或窗口有话要说时才留。

        常规的一轮没什么可说：条数与预算看 ``/status`` 末行，耗时也能由
        前后两条 ``ts`` 相减得到；每轮都写，覆盖层就只剩噪声。
        """
        if self._compact_error:
            return self._compact_error
        if window is None:
            return ""
        if not (
            window.dropped or window.note or self._compacted_now or self._pruned_now
        ):
            return ""
        note = window.describe()
        if self._compacted_now:
            note = "已压缩；" + note
        elif self._pruned_now:
            note = "已修剪；" + note
        return note

    # ---- 结果与回调 ----

    def _result(
        self,
        status: str,
        label: str,
        *,
        text: str = "",
        note: str = "",
        usage: Any = None,
        window: Any = None,
        stopped: BaseException | None = None,
    ) -> TurnResult:
        """把这一轮的结局收成一个结果；状态行备注在这里一次算好。"""
        return TurnResult(
            status=status,
            label=label,
            text=text,
            note=note,
            turn_note=self._turn_note(window),
            usage=usage,
            window=window,
            stopped=stopped,
        )

    def _phase(self, name: str) -> None:
        """把「这一轮在等什么」交给界面；没接回调时什么都不做。"""
        if self._on_phase is not None:
            self._on_phase(name)


def _is_summary(message: Any) -> bool:
    """这条消息是不是 ``fold`` 顶在头部的压缩摘要。"""
    return (message.content or "").startswith(SUMMARY_PREFIX)


def _context_overflow(error: Any) -> bool:
    """这个错误是不是「窗口装不下」？

    只认 4xx 里带明确措辞的那几种：把限流、鉴权也当成窗口问题，会白压掉一段
    历史，还把真正的原因藏起来。``ProviderError`` 的正文形如
    ``HTTP 400: {"error": ...}``。
    """
    text = str(error).lower()
    if "http 4" not in text:
        return False
    return any(hint in text for hint in _OVERFLOW_HINTS)


def _row(message: Any) -> str:
    """一条消息摊成纯文本；认不出的角色给空串（不进摘要）。"""
    if message.role == "user":
        return "用户：" + (message.content or "")
    if message.role == "assistant":
        rows = []
        if message.content:
            rows.append("助手：" + message.content)
        for call in message.tool_calls:
            rows.append("调用 {} {}".format(call.name, call.arguments))
        return "\n".join(rows)
    if message.role == "tool":
        return "结果：" + (message.content or "")
    return ""


def _clip_keep_ends(text: str, limit: int) -> str:
    """超过 ``limit`` 就头保尾留地剪，中间那句写明省略了多少字。

    只留尾巴会先切掉最早的原文 —— 那恰好是任务目标、路径、约定所在的位置，
    而且是模型的稳定前缀；留着它比留着最后一句寒暄值钱。
    """
    if len(text) <= limit:
        return text
    head = int(limit * COMPACT_SOURCE_HEAD_RATIO)
    tail = max(0, limit - head)
    omitted = len(text) - head - tail
    return "{}……（省略 {} 字）……\n{}".format(text[:head], omitted, text[-tail:])


def _transcript(messages: Any, limit: int = COMPACT_SOURCE_CHARS_MIN) -> str:
    """把要被压缩掉的消息摊成纯文本，供摘要请求使用。"""
    text = "\n".join(row for row in (_row(m) for m in messages) if row)
    return _clip_keep_ends(text, limit)


def _blocks(messages: Any, limit: int) -> List[list]:
    """按顺序分成若干块，每块摊成文本不超过 ``limit`` 字。

    按消息切，不按字符切：一条 tool 结果被腰斩，摘要就只能读到半句话。
    """
    blocks: List[list] = []
    current: list = []
    size = 0
    for message in messages:
        cost = len(_row(message)) + 1
        if current and size + cost > limit:
            blocks.append(current)
            current, size = [], 0
        current.append(message)
        size += cost
    if current:
        blocks.append(current)
    return blocks


def _compact_limits(dropped_tokens: int, limit: int = 0) -> Tuple[int, int]:
    """这次压缩的输入上限（字符）与摘要目标（字），都按被丢量伸缩。

    丢一千 token 时不必喂一个十万 token 的请求；丢十万 token 时也不该只喂
    两万字 —— 那样摘出来的要点对最早的任务一无所知，剩下的交给分块。``limit``
    是这条会话自己的预算：摘要请求也得装得进它，否则摘要请求自己先溢出。
    """
    source = int(dropped_tokens * COMPACT_SOURCE_CHARS_PER_TOKEN)
    source = max(COMPACT_SOURCE_CHARS_MIN, min(COMPACT_SOURCE_CHARS_MAX, source))
    if limit > 0:
        source = min(source, max(COMPACT_SOURCE_CHARS_MIN // 2, limit // 2))
    target = int(dropped_tokens * SUMMARY_TOKENS_RATIO)
    target = max(SUMMARY_TOKENS_MIN, min(SUMMARY_TOKENS_MAX, target))
    return source, max(120, int(target * SUMMARY_CHARS_PER_TOKEN))
