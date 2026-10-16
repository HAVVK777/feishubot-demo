"""飞书 → 核心：把事件翻译成命令。

四个必须在这里处理、不能外泄的关注点：

1. **两种请求体**：URL 校验和事件推送走同一个地址，靠 `type == "url_verification"`
   区分；
2. **两个不同的时限**：URL 校验要 **1 秒内**返回 `challenge`，事件推送要 **3 秒内**
   返回 200。后者靠"先 ACK、处理丢到后台"满足；
3. **回调和内容都是双重编码**：事件体在 `header` 里，消息正文在 `content` 里且是
   JSON **字符串**，要再解析一次；
4. **必须幂等**：飞书是"至少发送一次"，重复投递是正常的，不是异常。

签名（`X-Lark-Signature`）没有实现：它的算法依赖 Encrypt Key，而这个部署按明文
接收事件。生产环境应补上——校验来源目前只靠 Verification Token，而 token 是随
请求体一起来的，强度弱于签名。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from feishu_bot.core.domain import IncomingMessage
from feishu_bot.core.handler import MessageHandler
from feishu_bot.settings import Settings

logger = logging.getLogger(__name__)

EVENTS_PATH = "/feishu/events"
MESSAGE_EVENT = "im.message.receive_v1"


def build_router(handler: MessageHandler, settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.post(EVENTS_PATH)
    async def receive_event(request: Request, background: BackgroundTasks) -> Response:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="请求体不是 JSON") from None

        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体不是 JSON 对象")

        if "encrypt" in body:
            # 事件订阅地址也是"配置加密策略"的地方，所以后台很容易顺手打开
            # Encrypt Key。这里明确拒绝，好过返回 200 然后什么都不发生。
            raise HTTPException(
                status_code=501,
                detail=(
                    "收到加密事件，但本服务未启用解密。"
                    "请在飞书后台「事件与回调 → 加密策略」清空 Encrypt Key，"
                    "或在 inbound.py 补上解密分支。"
                ),
            )

        if body.get("type") == "url_verification":
            return _verify_url(body, settings)

        return _dispatch_event(body, settings, handler, background)

    return router


def _verify_url(body: dict[str, Any], settings: Settings) -> Response:
    """URL 校验：原样返回 challenge。

    这条路要 1 秒内响应，所以它是纯 CPU 的，没有任何 IO。

    注意飞书的首次校验请求**不带** `X-Lark-Signature` 头，所以这里也不能校验
    签名——在这里强制验签会让"配置请求地址"这一步直接失败。
    """
    if not _token_matches(body.get("token"), settings):
        raise HTTPException(status_code=403, detail="Verification Token 不匹配")

    challenge = body.get("challenge")
    if not isinstance(challenge, str):
        raise HTTPException(status_code=400, detail="url_verification 缺少 challenge")

    return JSONResponse({"challenge": challenge})


def _dispatch_event(
    body: dict[str, Any],
    settings: Settings,
    handler: MessageHandler,
    background: BackgroundTasks,
) -> Response:
    """事件推送：校验、解析、立刻 ACK，处理丢到响应之后。"""
    if body.get("schema") != "2.0":
        # 1.0 的旧格式没有顶层 schema，且元信息散在别处。这里不猜，明确记一条
        # 日志——静默丢弃的话，排查起来会完全没有线索。
        logger.warning("收到非 2.0 的事件体，已忽略：schema=%r", body.get("schema"))
        return Response(status_code=200)

    header = body.get("header")
    header = header if isinstance(header, dict) else {}

    if not _token_matches(header.get("token"), settings):
        raise HTTPException(status_code=403, detail="Verification Token 不匹配")

    if header.get("event_type") != MESSAGE_EVENT:
        logger.debug("忽略未订阅的事件类型：%r", header.get("event_type"))
        return Response(status_code=200)

    message = parse_message(body.get("event"))
    if message is None:
        return Response(status_code=200)

    # 先返回 200 再处理：飞书要求 3 秒内响应，超时就会重发。
    background.add_task(_handle_safely, handler, message)
    return Response(status_code=200)


async def _handle_safely(handler: MessageHandler, message: IncomingMessage) -> None:
    """在响应之后处理消息。

    必须自己吞掉异常：这里的异常发生在响应已经发出之后，Starlette 会把它抛到
    服务器层，日志看起来像"请求失败了"，而实际上飞书那边一切正常。
    """
    try:
        outcome = await handler.handle(message)
    except Exception:
        logger.exception("处理消息失败：%s", message.message_id)
        return
    logger.info("消息 %s 处理完成：%s", message.message_id, outcome)


def parse_message(event: Any) -> IncomingMessage | None:
    """把飞书的 `event` 体解析成 `IncomingMessage`。解析不了就返回 None。

    纯函数，所以事件体的各种形状都能直接单测。
    """
    if not isinstance(event, dict):
        return None

    raw = event.get("message")
    if not isinstance(raw, dict):
        return None

    message_id = raw.get("message_id")
    if not message_id:
        # 没有 message_id 就无法去重，重复投递会导致重复回复。宁可漏回一条，
        # 也不要发出两条一样的卡片。
        logger.warning("事件里没有 message_id，已忽略")
        return None

    message_type = raw.get("message_type")
    if message_type != "text":
        logger.info("跳过非文本消息：message_type=%r", message_type)
        return None

    sender = event.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    sender_id = sender.get("sender_id")
    sender_id = sender_id if isinstance(sender_id, dict) else {}

    return IncomingMessage(
        message_id=str(message_id),
        chat_id=str(raw.get("chat_id") or ""),
        sender_id=str(sender_id.get("open_id") or ""),
        text=_extract_text(raw),
        chat_type=str(raw.get("chat_type") or ""),
    )


def _extract_text(message: dict[str, Any]) -> str:
    """取出正文。

    `content` 是**字符串化的 JSON**（例如 `'{"text":"你好"}'`），要再解析一次。
    这是事件订阅里最常见的"字段明明是空的"的原因。
    """
    raw = message.get("content")
    if not isinstance(raw, str):
        return ""

    try:
        content = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("content 不是合法 JSON：%r", raw[:200])
        return ""

    if not isinstance(content, dict):
        return ""

    text = content.get("text")
    if not isinstance(text, str):
        return ""

    return _strip_mentions(text, message.get("mentions")).strip()


def _strip_mentions(text: str, mentions: Any) -> str:
    """去掉 @机器人 留下的占位符。

    群聊里正文是 `"@_user_1 你好"`，`mentions` 里的 `key` 就是那个 `@_user_1`。
    不处理的话，用户发给机器人的每个问题前面都会拖着一个无意义的占位符。
    """
    if not isinstance(mentions, list):
        return text

    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        key = mention.get("key")
        if isinstance(key, str) and key:
            text = text.replace(key, "")

    return text


def _token_matches(token: Any, settings: Settings) -> bool:
    """Verification Token 校验。没配就跳过（本地演示的默认状态）。"""
    if not settings.verification_token:
        return True
    return token == settings.verification_token
