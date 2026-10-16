"""用例：一条入站消息 → 路由 → 分发到服务 → 一条回复。

流程本身很短，值得写清楚每一步的**顺序为什么是这个顺序**：

1. **去重排在最前**。路由是纯 CPU 的，但服务有真实的副作用（一次外呼就是一个
   副作用），重复投递绝不能变成重复调用。
2. **路由，然后分发**。命中的关键字就是注册表里的键，取出实现，把内容交给它。
3. **发送放在最后**，且不在兜底 `try` 里——发送失败是基础设施问题，要抛出去让
   入站适配器记录，而不是被当成"服务失败"再回一张卡片（那张卡片同样发不出去）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from feishu_bot.core.domain import IncomingMessage, Reply, ServiceRequest, Tone
from feishu_bot.core.ports import Messenger, SeenStore, Service
from feishu_bot.core.routing import Router, UnknownCommand

logger = logging.getLogger(__name__)

FAILED_TITLE = "服务暂时不可用"
UNREADABLE_TITLE = "没有收到文字"


class Outcome(StrEnum):
    """处理结果。测试直接断言这个，不用去翻日志。"""

    #: 同一条消息的重复投递，已按幂等规则忽略。
    DUPLICATE = "duplicate"
    #: 正文读不出来（content 不是合法 JSON，或者只有空白）。
    UNREADABLE = "unreadable"
    #: 已分发到服务并回复。
    HANDLED = "handled"
    #: 命令命中但没给内容，回了用法提示，没有调用服务。
    USAGE = "usage"
    #: 没命中规则，或者命令不认识，回了帮助卡片。
    HELP = "help"
    #: 服务抛异常，回了兜底卡片。
    FAILED = "failed"


@dataclass(slots=True)
class MessageHandler:
    messenger: Messenger
    seen: SeenStore
    router: Router
    #: 关键字 → 实现。覆盖性由 `bootstrap` 在启动时校验。
    services: Mapping[str, Service]

    async def handle(self, message: IncomingMessage) -> Outcome:
        if not self.seen.claim(message.dedup_key):
            return Outcome.DUPLICATE

        reply, outcome = await self._compose_reply(message)
        await self.messenger.send(message.chat_id, reply)
        return outcome

    async def _compose_reply(self, message: IncomingMessage) -> tuple[Reply, Outcome]:
        """决定回什么。

        拆出来是为了让 `handle` 保持在三行内——扁平写法会把
        `await self.messenger.send(...)` 在每个分支里各抄一遍。
        """
        text = message.text.strip()
        if not text:
            return _unreadable_reply(), Outcome.UNREADABLE

        routed = self.router.route(text)
        if isinstance(routed, UnknownCommand):
            return self.router.help_reply(unknown=routed.token), Outcome.HELP
        if routed is None:
            return self.router.help_reply(), Outcome.HELP

        rule = routed.rule
        if rule.requires_argument and not routed.argument:
            return self.router.usage_reply(rule), Outcome.USAGE

        service = self.services.get(rule.keyword)
        if service is None:
            # bootstrap 已经校验过覆盖性，走到这里说明校验漏了。
            logger.error("关键字 %s 没有对应的服务——bootstrap 的覆盖校验漏了", rule.keyword)
            return _failed_reply(message), Outcome.FAILED

        logger.info(
            "消息 %s 路由到 %s（%s，触发词 %r）",
            message.message_id,
            rule.keyword,
            routed.matched,
            routed.trigger,
        )
        try:
            reply = await service.handle(
                ServiceRequest(
                    message=message,
                    argument=routed.argument,
                    matched=routed.matched,
                    trigger=routed.trigger,
                )
            )
        except Exception:
            # CancelledError 是 BaseException，不会被这里吞掉——取消照旧要传播。
            logger.exception(
                "服务执行失败：keyword=%s message_id=%s", rule.keyword, message.message_id
            )
            return _failed_reply(message), Outcome.FAILED

        return reply, Outcome.HANDLED


def _unreadable_reply() -> Reply:
    """比静默丢弃好：用户在界面上看得到有东西来过，日志里有线索，排查有起点。"""
    return Reply(
        title=UNREADABLE_TITLE,
        body="这条消息里没有可读的文字内容。",
        tone=Tone.WARNING,
    )


def _failed_reply(message: IncomingMessage) -> Reply:
    """服务挂了时给用户的兜底。带上消息 ID，方便对照日志。"""
    return Reply(
        title=FAILED_TITLE,
        body=f"处理这条消息时出错了，稍后再试。\n\n消息 ID `{message.message_id}`",
        tone=Tone.WARNING,
    )
