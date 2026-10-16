"""核心 → 飞书：把结果翻译成消息 / 卡片。

卡片 JSON 是飞书特有的结构，只在这个文件里构造。核心拿到的是中立的 message
对象，所以换卡片版本或者以后接第二个渠道时，改的是这里，不是用例。

两个容易踩的坑，都在这里处理掉了：

* `content` 必须是**字符串化的 JSON**，不能直接传对象——这是发送失败最常见的原因；
* `tenant_access_token` 有效期 2 小时，每次发送都去换会被限频，所以缓存住。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from feishu_bot.core.domain import Reply, Tone
from feishu_bot.settings import Settings

logger = logging.getLogger(__name__)

TENANT_ACCESS_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
SEND_MESSAGE_PATH = "/open-apis/im/v1/messages"

#: 语气 → 卡片配色。这张表就是"核心的表达"落到"飞书的样子"的那个点：
#: 核心说 WARNING，这里决定它是橙色。
CARD_TEMPLATE = {
    Tone.INFO: "blue",
    Tone.SUCCESS: "green",
    Tone.WARNING: "orange",
}

#: token 有效期 2 小时。提前 5 分钟换，免得正好踩在过期边界上。
TOKEN_REFRESH_MARGIN_SECONDS = 300


class FeishuAPIError(RuntimeError):
    """飞书接口返回了非 0 的业务错误码。"""

    def __init__(self, code: Any, msg: str, *, action: str) -> None:
        super().__init__(f"飞书接口调用失败（{action}）：code={code} msg={msg}")
        self.code = code
        self.msg = msg
        self.action = action


#: 长得像标签的 `<`：后面紧跟字母或斜杠。见 `_neutralise_tags`。
_TAG_LIKE = re.compile(r"<(?=[a-zA-Z/])")
_SAFE_LEFT_ANGLE = "＜"


def _neutralise_tags(text: str) -> str:
    """把**长得像标签的** `<` 换成全角，让 `<at>` 这类标签拼不出来。

    卡片正文用的是 `lark_md`，而 `lark_md` 认 `<at id=...>` 这类标签。正文里却可能
    混着用户原文——外部服务把用户的问题回显进结果是最常见的情况。不处理的话，一条
    精心构造的消息就能让机器人替发信人 @ 别人。

    **全部卡片正文都在这里过一遍**，而不是在每个服务里各做一次：这是一条与数据来源
    无关的规则，放在出口就没人能忘。

    只认"`<` 后面跟字母或斜杠"这一种形状，是因为一刀切换掉所有 `<` 会误伤自己的文案：
    帮助卡片里的 `/搜索 <内容>` 占位符、以及正文里的 `a < b` 都会变形。而 `lark_md`
    认识的标签（`<at>` / `<a>` / `<font>` …）全都是以 ASCII 字母开头的，按这个形状
    判断既够用又不会误伤。

    标题不受影响：它用的是 `plain_text`，本来就不解析标签。
    """
    return _TAG_LIKE.sub(_SAFE_LEFT_ANGLE, text)


def build_card(reply: Reply) -> dict[str, Any]:
    """把一条中立的回复渲染成飞书卡片。

    纯函数，所以卡片长什么样可以直接单测，不用起服务、不用发请求。
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": reply.title},
            "template": CARD_TEMPLATE.get(reply.tone, "blue"),
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": _neutralise_tags(reply.body)}},
        ],
    }


class FeishuMessenger:
    """`Messenger` 的飞书实现。"""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._token: str | None = None
        self._token_expires_at = 0.0
        # 并发发送时只让一个协程去换 token，其余的等它换回来。
        self._token_lock = asyncio.Lock()

    async def send(self, chat_id: str, reply: Reply) -> None:
        token = await self._access_token()
        response = await self._client.post(
            SEND_MESSAGE_PATH,
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "interactive",
                # 必须序列化成字符串，不能直接放对象。
                "content": json.dumps(build_card(reply), ensure_ascii=False),
            },
        )
        payload = _payload(response)
        if payload.get("code") != 0:
            raise FeishuAPIError(payload.get("code"), payload.get("msg", ""), action="发送消息")

    async def _access_token(self) -> str:
        if self._token is not None and time.monotonic() < self._token_expires_at:
            return self._token

        async with self._token_lock:
            # 等锁期间可能已经有别的协程换好了，再查一次。
            if self._token is not None and time.monotonic() < self._token_expires_at:
                return self._token

            response = await self._client.post(
                TENANT_ACCESS_TOKEN_PATH,
                json={
                    "app_id": self._settings.app_id,
                    "app_secret": self._settings.app_secret,
                },
            )
            payload = _payload(response)
            if payload.get("code") != 0:
                raise FeishuAPIError(
                    payload.get("code"), payload.get("msg", ""), action="获取 tenant_access_token"
                )

            self._token = payload["tenant_access_token"]
            self._token_expires_at = (
                time.monotonic() + payload["expire"] - TOKEN_REFRESH_MARGIN_SECONDS
            )
            return self._token


def _payload(response: httpx.Response) -> dict[str, Any]:
    """取出响应体，并把它变成"能判断失败"的形状。

    飞书对业务错误也返回 HTTP 200，错误码在 body 的 `code` 里，所以不能只看
    状态码。这里只负责把 body 拿到手，`code` 的判断交给调用方。
    """
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise FeishuAPIError(
            None, f"响应不是 JSON：{response.text[:200]}", action="解析响应"
        ) from exc
    if not isinstance(payload, dict):
        raise FeishuAPIError(None, f"响应不是对象：{payload!r}", action="解析响应")
    return payload
