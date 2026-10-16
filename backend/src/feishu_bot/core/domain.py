"""核心的领域类型。

放进这里的类型只有 dataclass 和它自己的不变量检查——不 import 飞书 SDK，也不
import web 框架。好处是 `tests/` 里可以直接构造它们，不需要任何租户凭据。

注意 `Reply` 里没有"卡片""颜色"这类字样：核心只决定**说什么**（标题、正文、
语气），不决定**长什么样**。把语气翻成飞书的卡片配色是出站适配器的事。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Tone(StrEnum):
    """回复的语气。取值是业务含义，不是飞书的模板名。"""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"


class MatchKind(StrEnum):
    """一条消息是怎么被认出来的。服务可以据此调整行为。"""

    #: 斜杠形式：`/搜索 内容`。内容就是斜杠之后的那段。
    SLASH = "slash"
    #: 正文里出现了触发词：`帮我搜索一下`。内容是整条消息。
    MENTION = "mention"


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """一条归一化之后的入站消息。

    到这里为止，飞书那套 `schema` / `header` / 双重 JSON 编码的 `content`
    已经被入站适配器剥掉了。
    """

    message_id: str
    chat_id: str
    sender_id: str
    text: str
    #: 飞书的原始取值："p2p"（单聊）或 "group"（群聊）。
    #: `is_group` 是领域层对它的解释——服务不应该自己去比这个字符串。
    chat_type: str

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"

    @property
    def dedup_key(self) -> str:
        """幂等键。

        用 `message_id` 而不是 `event_id`：飞书的 IM 文档明确要求按 `message_id`
        去重，理由是 `event_id` 在某些情况下会是空字符串。落在 `message_id` 上也
        更贴合我们真正要防的后果——同一条消息被回复两遍。
        """
        return self.message_id


@dataclass(frozen=True, slots=True)
class Reply:
    """一条待发出的回复。核心能表达的全部内容。"""

    title: str
    body: str
    tone: Tone = Tone.INFO


@dataclass(frozen=True, slots=True)
class ServiceRequest:
    """一次服务调用的全部输入。

    整个 `IncomingMessage` 都传进来，而不是只给一截文本：外呼服务以后可能要取
    上下文（`chat_id`）或者做个性化（`sender_id`），现在不给的话，将来只能改协议。
    """

    message: IncomingMessage
    #: 要交给服务的正文。斜杠形式是斜杠之后的那段；正文触发是整条消息。
    #: 契约保证非空——没给内容的调用在用例层就被拦成用法提示了。
    argument: str
    matched: MatchKind
    #: 实际认出来的那个词。斜杠形式就是关键字本身，正文触发时是命中的别名。
    trigger: str
