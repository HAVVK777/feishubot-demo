"""把命中的内容转发到外部服务，把响应翻译成卡片。

这里是对外接口的**唯一定义处**：请求长什么样、响应怎么读、什么算失败，全部收敛在
这个文件。以后要换协议（加签名、改字段名、支持 GET），改这里就够了。

## 接口约定

**请求**（`Content-Type: application/json`）：

```json
{
  "keyword": "搜索",
  "text": "transformer",
  "messageId": "om_xxx",
  "chatId": "oc_xxx",
  "senderId": "ou_xxx",
  "matched": "slash",
  "trigger": "搜索"
}
```

`text` 是真正要处理的内容：斜杠形式取 `/关键字 ` 之后的那段，正文触发时是整条消息。
其余字段是来源信息，用不上的服务忽略即可。

**响应**：

```json
{
  "title": "检索结果",
  "text": "找到 3 篇相关论文…",
  "tone": "success"
}
```

`text` 必填（空字符串或缺失都算失败），`title` 缺省用关键字，`tone` 取
`info` / `success` / `warning`，认不出来当 `info`。

**失败**：非 2xx、响应不是 JSON、`text` 缺失——都抛 `ServiceCallError`，
由用例兜底成"服务暂时不可用"的卡片。想表达"没查到结果"不要用错误码，
正常返回一条 `text` 就行，语气自己定。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import httpx

from feishu_bot.core.domain import Reply, ServiceRequest, Tone

logger = logging.getLogger(__name__)

#: 外部服务返回的语气字符串 → 我们的语气。认不出来当 INFO——不让对方的一个拼写
#: 错误把整条回复搞挂。
_TONES = {tone.value: tone for tone in Tone}


class ServiceCallError(RuntimeError):
    """外部服务调用失败。用例会把它兜底成"服务暂时不可用"的卡片。"""

    def __init__(self, message: str, *, endpoint: str) -> None:
        super().__init__(f"外部服务调用失败（{endpoint}）：{message}")
        self.endpoint = endpoint


class HttpService:
    """`Service` 的 HTTP 实现：一次命中 = 一次 POST。"""

    def __init__(
        self,
        *,
        keyword: str,
        endpoint: str,
        client: httpx.AsyncClient,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._keyword = keyword
        self._endpoint = endpoint
        self._client = client
        self._headers = dict(headers or {})
        self._timeout = timeout_seconds

    async def handle(self, request: ServiceRequest) -> Reply:
        payload = {
            "keyword": self._keyword,
            "text": request.argument,
            "messageId": request.message.message_id,
            "chatId": request.message.chat_id,
            "senderId": request.message.sender_id,
            "matched": request.matched.value,
            "trigger": request.trigger,
        }

        logger.info(
            "转发到 %s（keyword=%s，text=%r）", self._endpoint, self._keyword, request.argument
        )
        try:
            response = await self._client.post(
                self._endpoint,
                headers=self._headers,
                json=payload,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # 连不上、超时、DNS 失败都归这里。带上原文，否则排查只剩"失败了"。
            raise ServiceCallError(f"请求发不出去：{exc}", endpoint=self._endpoint) from exc

        payload_out = _parse(response, endpoint=self._endpoint)
        return _to_reply(payload_out, keyword=self._keyword, endpoint=self._endpoint)


def _parse(response: httpx.Response, *, endpoint: str) -> dict[str, Any]:
    if not response.is_success:
        raise ServiceCallError(
            f"HTTP {response.status_code}：{response.text[:200]}", endpoint=endpoint
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ServiceCallError(f"响应不是 JSON：{response.text[:200]}", endpoint=endpoint) from exc

    if not isinstance(payload, dict):
        raise ServiceCallError(f"响应不是对象：{payload!r}", endpoint=endpoint)

    return payload


def _to_reply(payload: dict[str, Any], *, keyword: str, endpoint: str) -> Reply:
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ServiceCallError('响应缺少非空的 "text" 字段', endpoint=endpoint)

    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        # 没给标题就用关键字——用户至少知道是哪条命令回的话。
        title = keyword

    tone = _TONES.get(str(payload.get("tone", "")).strip().lower(), Tone.INFO)
    return Reply(title=title, body=text, tone=tone)
