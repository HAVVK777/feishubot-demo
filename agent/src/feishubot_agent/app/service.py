"""agent 主体：一条 backend 请求 → 一个回合。

回合的生命周期：

1. `registry.get(chat_id)`；
2. 在有限时间内拿到会话锁（拿不到就回「上一条还在处理」）；
3. 裁剪历史 → `run_turn`；
4. 成功后把用户消息和本回合新增消息追加进会话历史；
5. `to_reply`；
6. 释放锁。

第 4 步只在回合成功后写历史。模型失败、超时、预算耗尽时不要把半截回合写进去，
否则下一次请求会看到一条没有结果的 user 消息。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from datetime import date
from typing import Protocol

from feishubot_agent.app.contracts import TurnReply, TurnRequest
from feishubot_agent.app.profile import AgentProfile
from feishubot_agent.app.reply import to_reply
from feishubot_agent.harness.context import trim_history
from feishubot_agent.harness.errors import BudgetExceeded, ModelError
from feishubot_agent.harness.loop import run_turn
from feishubot_agent.harness.model import ModelClient, ToolSpec, UserMessage
from feishubot_agent.harness.policy import TurnBudget
from feishubot_agent.harness.session import Session, SessionRegistry
from feishubot_agent.harness.tools import ToolExecutor

logger = logging.getLogger(__name__)

type ToolExecutorFactory = Callable[[str, str], ToolExecutor]
"""`(session_id, chat_type) -> ToolExecutor`。app 层把会话标签绑到 MCP 客户端上。"""


class TurnService(Protocol):
    async def handle(self, request: TurnRequest) -> TurnReply: ...


class AgentTurnService:
    """v1 的回合服务。依赖全部由 `bootstrap.py` 注入。"""

    def __init__(
        self,
        *,
        profile: AgentProfile,
        registry: SessionRegistry,
        model: ModelClient,
        tools: Sequence[ToolSpec],
        executor_factory: ToolExecutorFactory,
        turn_timeout_seconds: float,
        history_max_turns: int,
        session_lock_wait_seconds: float,
    ) -> None:
        self._profile = profile
        self._registry = registry
        self._model = model
        self._tools = tuple(tools)
        self._executor_factory = executor_factory
        self._turn_timeout_seconds = turn_timeout_seconds
        self._history_max_turns = history_max_turns
        self._session_lock_wait_seconds = session_lock_wait_seconds

    async def handle(self, request: TurnRequest) -> TurnReply:
        if not request.text.strip():
            return _warning(self._profile.reply_title, "请提供要处理的内容。")

        started = time.monotonic()
        session = self._registry.get(request.chat_id)

        try:
            await asyncio.wait_for(
                session.lock.acquire(),
                timeout=self._session_lock_wait_seconds,
            )
        except TimeoutError:
            logger.info(
                "会话 %s 上一条消息仍在处理，拒绝并发请求（message=%s）",
                request.chat_id,
                request.message_id,
            )
            return _warning(self._profile.reply_title, "上一条消息还在处理，请稍后再试。")

        try:
            reply = await self._handle_locked(request, session)
        finally:
            session.lock.release()
            logger.info(
                "请求结束（chat=%s，message=%s，%.0f ms）",
                request.chat_id,
                request.message_id,
                (time.monotonic() - started) * 1000,
            )
        return reply

    async def _handle_locked(self, request: TurnRequest, session: Session) -> TurnReply:
        history = trim_history(
            [*session.messages, UserMessage(request.text)],
            max_turns=self._history_max_turns,
        )
        budget = TurnBudget.start(
            max_steps=self._profile.max_steps,
            timeout_seconds=self._turn_timeout_seconds,
        )
        executor = self._executor_factory(request.chat_id, request.chat_type)

        # open_id 是 backend 从已验证的飞书事件里带出来的，不是模型或用户自称的。
        # 它是**上下文**，不是授权：需要身份校验的工具必须在工具内部自己做权限判断。
        system = self._profile.system_prompt
        if request.sender_id:
            system += f"\n\n当前用户的 open_id：{request.sender_id}"
        system += f"\n\n当前日期：{date.today().isoformat()}。用户说「这个月」「今天」时以此为准。"

        try:
            async with asyncio.timeout(self._turn_timeout_seconds):
                result = await run_turn(
                    model=self._model,
                    system=system,
                    history=history,
                    tools=self._tools,
                    executor=executor,
                    budget=budget,
                )
        except TimeoutError:
            logger.warning("回合超时（chat=%s，message=%s）", request.chat_id, request.message_id)
            # 超时只表示「我们不等了」，不表示工具没执行。不要鼓励用户直接重试一个
            # 可能有副作用的操作。
            return _warning(
                self._profile.reply_title,
                "这次处理超时了，部分操作可能仍在执行；请稍后确认结果再重试。",
            )
        except BudgetExceeded as exc:
            logger.warning(
                "回合超出预算（chat=%s，message=%s）：%s",
                request.chat_id,
                request.message_id,
                exc,
            )
            return _warning(
                self._profile.reply_title,
                "这次处理步骤太多了，请把问题拆小一点再试。",
            )
        except ModelError as exc:
            logger.warning(
                "模型调用失败（chat=%s，message=%s）：%s",
                request.chat_id,
                request.message_id,
                exc,
            )
            return _warning(self._profile.reply_title, "模型服务暂时不可用，请稍后再试。")
        except Exception:
            logger.exception(
                "回合处理失败（chat=%s，message=%s）",
                request.chat_id,
                request.message_id,
            )
            return _warning(
                self._profile.reply_title,
                "处理这条消息时出错了，请稍后再试。",
            )

        if not result.text.strip():
            # 空 assistant 消息留在历史里会让下一次请求带着一个空的 assistant turn；
            # 这条回合没有可用的内容，干脆不写历史。
            logger.warning(
                "回合没有产生可回复文本，历史不写入（chat=%s，message=%s）",
                request.chat_id,
                request.message_id,
            )
            return to_reply(result.text, title=self._profile.reply_title)

        session.messages = trim_history(
            [
                *session.messages,
                UserMessage(request.text),
                *result.new_messages,
            ],
            max_turns=self._history_max_turns,
        )
        logger.info(
            "回合完成（chat=%s，message=%s，steps=%d，stop=%s）",
            request.chat_id,
            request.message_id,
            result.steps,
            result.stop_reason,
        )
        return to_reply(result.text, title=self._profile.reply_title)


def _warning(title: str, text: str) -> TurnReply:
    return TurnReply(title=title, text=text, tone="warning")
