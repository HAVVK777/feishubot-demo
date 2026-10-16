"""关键字 → 服务 的路由表。

这一层是纯数据和纯函数：不 import 飞书、不 import 框架、不发 IO。所以"一句话会被
路由到哪"可以直接单测——注册一个新关键字不用起服务，也不用凭据。

**关键字就是路由的身份。** 每条规则的关键字既是斜杠命令的词（`/搜索 内容`），也是它
在服务注册表里的键。所以"加一个功能"= 加一条规则 + 给它一个服务，两者用同一个字符串
对上，不需要在别处再维护一份映射。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from feishu_bot.core.domain import MatchKind, Reply, Tone

logger = logging.getLogger(__name__)

#: 命令前缀。全角斜杠在中文输入法下很容易打出来，为此回一张"未知命令"太不友好。
COMMAND_PREFIXES = "/／"

HELP_TITLE = "我能做什么"
USAGE_TITLE = "这个命令需要参数"

#: 内置的 `/帮助`。它固定存在，配置里的关键字和它撞了会被拒绝（见 `check_collisions`）。
HELP_KEYWORD = "帮助"


@dataclass(frozen=True, slots=True)
class Rule:
    """一个触发条件。

    每个关键字对应**一个**服务：`keyword` 既用来匹配命令，也用来在注册表里查实现。
    """

    #: 不带斜杠。同时也是服务注册表里的键。
    keyword: str
    #: 额外的正文触发词。留空表示只能通过 `/关键字 内容` 触发。
    #: 填了之后，正文里出现这些词也会路由，此时内容算整条消息。
    aliases: tuple[str, ...] = ()
    #: 帮助卡片里的一句话。
    description: str = ""
    #: 大者优先。默认全 0，靠"最长别名 → 声明顺序"兜底。
    priority: int = 0
    #: 斜杠形式没给内容时，是回用法提示还是照样调用服务。
    requires_argument: bool = True

    def __post_init__(self) -> None:
        """坏规则要在启动时就炸，不是等第一条消息进来。"""
        if not self.keyword.strip():
            raise ValueError("关键字不能是空白")
        if any(char in COMMAND_PREFIXES for char in self.keyword):
            raise ValueError(f"关键字不带斜杠，写 {self.keyword!r} 而不是 /{self.keyword!r}")
        if any(char.isspace() for char in self.keyword):
            raise ValueError(f"关键字不含空白：{self.keyword!r}")
        if any(not alias.strip() for alias in self.aliases):
            raise ValueError("别名不能是空白——它会命中每一条消息")

    @property
    def usage(self) -> str:
        """帮助和用法提示里的写法。需要内容的关键字统一写成 `<内容>`。"""
        return f"/{self.keyword} <内容>" if self.requires_argument else f"/{self.keyword}"


BUILTIN_RULES: tuple[Rule, ...] = (
    Rule(keyword=HELP_KEYWORD, requires_argument=False, description="显示这份帮助"),
)


@dataclass(frozen=True, slots=True)
class Hit:
    """命中了一条规则。"""

    rule: Rule
    #: 斜杠形式是斜杠之后的内容；正文触发是整条消息。
    argument: str
    matched: MatchKind
    #: 实际认出来的那个词——斜杠形式就是关键字本身，正文触发时是命中的别名。
    trigger: str


@dataclass(frozen=True, slots=True)
class UnknownCommand:
    """斜杠开头，但表里没有这个关键字。

    单独一个类型而不是返回 None：用户打 `/sousuo` 最可能是拼错了，回一张带
    "没有这个命令"的帮助卡片比静默按正文触发有用得多——后者会把拼错的词原样交给
    某个服务，还掩盖了错误。
    """

    token: str


class Router:
    """把一句话变成一条命中，或者"我不认识"。"""

    def __init__(self, rules: Sequence[Rule]) -> None:
        self._rules: tuple[Rule, ...] = tuple(rules)
        self._warn_about_ambiguous_aliases()

    @property
    def rules(self) -> tuple[Rule, ...]:
        return self._rules

    def route(self, text: str) -> Hit | UnknownCommand | None:
        """三步，顺序即语义：命令趟 → 别名趟 → 选一。"""
        text = text.strip()
        if not text:
            return None

        command = _split_command(text)
        if command is not None:
            token, argument = command
            # 命令趟结束后**不管结果如何都不再走别名趟**：斜杠是用户明确的
            # "我在下命令"信号，词不对就该看见提示。
            return self._match_command(token, argument)

        return self._match_aliases(text)

    def help_reply(self, *, unknown: str | None = None) -> Reply:
        """帮助卡片。**每次从规则表现算**——注册一个关键字，帮助自动跟着变。

        正文只用纯文本和 `-` 项目符号，不假设它会被渲染成富文本：core 不知道
        出站适配器会把它放进飞书卡片还是纯文本消息，换渠道后这份帮助同样能看。
        """
        lines: list[str] = []
        if unknown:
            lines.append(f"没有「/{unknown}」这个命令。")
            lines.append("")

        lines.append("可用功能（命令或关键词都能触发）：")
        for rule in self._rules:
            line = f"- {rule.usage}"
            if rule.description:
                line += f"：{rule.description}"
            if rule.aliases:
                line += f"（也可以直接说：{'、'.join(rule.aliases)}）"
            lines.append(line)

        return Reply(title=HELP_TITLE, body="\n".join(lines), tone=Tone.INFO)

    def usage_reply(self, rule: Rule) -> Reply:
        """斜杠形式但没给内容。用法文案从规则表来，所以只有一处需要维护。"""
        lines = [f"用法：{rule.usage}"]
        if rule.description:
            lines.append(rule.description)
        return Reply(title=USAGE_TITLE, body="\n".join(lines), tone=Tone.WARNING)

    def _match_command(self, token: str, argument: str) -> Hit | UnknownCommand:
        folded = token.casefold()
        for rule in self._rules:
            if rule.keyword.casefold() == folded:
                return Hit(
                    rule=rule,
                    argument=argument.strip(),
                    matched=MatchKind.SLASH,
                    trigger=rule.keyword,
                )

        logger.info("未知命令：/%s", token)
        return UnknownCommand(token)

    def _match_aliases(self, text: str) -> Hit | None:
        folded_text = text.casefold()
        hits: list[Hit] = []
        for rule in self._rules:
            alias = _longest_match(rule.aliases, folded_text)
            if alias is not None:
                hits.append(Hit(rule=rule, argument=text, matched=MatchKind.MENTION, trigger=alias))

        if not hits:
            return None

        # max 返回**第一个**取到最大值的元素，所以三级 tiebreak 里的最后一级
        # "声明顺序"是 max 的天然行为，不需要写出第三级键。
        chosen = max(hits, key=lambda hit: (hit.rule.priority, len(hit.trigger)))

        if len(hits) > 1:
            # 多条命中是异常情况，整条命中列表进 INFO——只记"选中了谁"的话，
            # 事后没法知道当时还有哪些规则也匹配上了。
            logger.info(
                "一条消息命中 %d 条规则：%s；选中 %s（priority=%d，别名=%r）",
                len(hits),
                "、".join(f"{hit.rule.keyword}/{hit.trigger}" for hit in hits),
                chosen.rule.keyword,
                chosen.rule.priority,
                chosen.trigger,
            )
        else:
            # 单条命中是常态，放 DEBUG，否则日志会被每一句话刷屏。
            logger.debug("别名命中 %s（别名=%r）", chosen.rule.keyword, chosen.trigger)

        return chosen

    def _warn_about_ambiguous_aliases(self) -> None:
        """两条规则用了同一个别名且优先级相同——声明顺序会静默决定谁赢。

        不报错：这不一定是个错误（可能是有意的兜底规则）。但优先级规则让这种歧义
        变成静默行为，启动时留一条线索是值得的。
        """
        seen: dict[str, Rule] = {}
        for rule in self._rules:
            for alias in rule.aliases:
                other = seen.setdefault(alias.casefold(), rule)
                if other is not rule and other.priority == rule.priority:
                    logger.warning(
                        "别名 %r 同时属于 %s 和 %s 且优先级相同，声明在前的生效",
                        alias,
                        other.keyword,
                        rule.keyword,
                    )


def check_collisions(rules: Sequence[Rule]) -> None:
    """配置里的关键字和内置关键字不能撞车，两个配置之间也不能。

    在 `Router` 外面单独一个函数，是因为这是**配置**的问题而不是路由的问题：
    `Router` 本身完全接受两张重名的规则（比如测试里要构造歧义场景）。
    """
    seen: dict[str, str] = {}
    for rule in rules:
        folded = rule.keyword.casefold()
        if folded in seen:
            raise ValueError(f"关键字 {rule.keyword!r} 重复了")
        seen[folded] = rule.keyword


def _split_command(text: str) -> tuple[str, str] | None:
    """把 `/搜索 内容` 拆成 `("搜索", "内容")`。

    不是命令就返回 None。判据只看开头是不是斜杠——那之后的所有空白字符（含全角
    空格 U+3000）都算分隔符，`str.split` 天然覆盖。
    """
    if text[0] not in COMMAND_PREFIXES:
        return None

    # 连续多个斜杠也当命令处理（`//搜索 x`）。这是宽容带来的副作用，无害，
    # 不值得为它加一条判断。
    stripped = text.lstrip(COMMAND_PREFIXES)
    parts = stripped.split(maxsplit=1)

    token = parts[0] if parts else ""
    if not token:
        # 光一个 "/"，没有关键字可言，回"未知命令"没有提示价值。
        return None

    return token, parts[1] if len(parts) > 1 else ""


def _longest_match(aliases: tuple[str, ...], folded_text: str) -> str | None:
    """返回命中的别名**原文**（不是 casefold 之后的）。

    同一条规则命中多个别名时只留最长的：日志里同一条规则出现两次没有意义，而最长
    的那个才是"更具体"的线索。
    """
    matches = [alias for alias in aliases if alias.casefold() in folded_text]
    return max(matches, key=len) if matches else None
