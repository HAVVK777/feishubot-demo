"""路由表的测试。

纯 core：没有飞书字样，没有网络，没有凭据。注册一个关键字之后"哪句话会被路由到哪"
在这里就能验完。
"""

from __future__ import annotations

import logging

import pytest

from feishu_bot.core.domain import MatchKind, Tone
from feishu_bot.core.routing import (
    HELP_KEYWORD,
    Hit,
    Router,
    Rule,
    UnknownCommand,
    check_collisions,
)

ROUTING_LOGGER = "feishu_bot.core.routing"


def make_rules() -> tuple[Rule, ...]:
    """和 tests/conftest.py 里那份 routes.json 同形。"""
    return (
        Rule(
            keyword="搜索", aliases=("搜索", "查一下", "搜一下"), description="在当前知识库里检索"
        ),
        Rule(keyword="摘要", description="把一段内容压缩成要点"),
        Rule(keyword=HELP_KEYWORD, requires_argument=False, description="显示这份帮助"),
    )


def make_router(*rules: Rule) -> Router:
    return Router(rules if rules else make_rules())


class TestSlashForm:
    def test_keyword_and_argument_split_on_the_first_space(self) -> None:
        hit = make_router().route("/搜索 transformer 论文")

        assert isinstance(hit, Hit)
        assert hit.rule.keyword == "搜索"
        assert hit.argument == "transformer 论文"
        assert hit.matched is MatchKind.SLASH
        # 斜杠形式下触发词就是关键字本身。
        assert hit.trigger == "搜索"

    def test_trailing_whitespace_is_trimmed_from_the_argument(self) -> None:
        assert make_router().route("/搜索   transformer   ").argument == "transformer"

    def test_keyword_without_argument_still_hits(self) -> None:
        """路由只负责给出空内容；"该不该回用法提示"是用例层的事。"""
        hit = make_router().route("/搜索")

        assert isinstance(hit, Hit)
        assert hit.argument == ""

    def test_full_width_slash_and_space_are_accepted(self) -> None:
        """中文输入法下这两个全角字符很容易打出来。"""
        hit = make_router().route("／搜索　transformer")

        assert isinstance(hit, Hit)
        assert hit.rule.keyword == "搜索"
        assert hit.argument == "transformer"

    def test_keyword_matching_is_case_insensitive(self) -> None:
        hit = make_router(Rule(keyword="search")).route("/Search x")

        assert hit.rule.keyword == "search"

    def test_glued_keyword_is_unknown_not_a_mention(self) -> None:
        """`/搜索transformer` 是打错了，不该悄悄退化成正文触发。

        退化的话，这一整串会被原样交给搜索服务，而用户永远看不到"你命令写错了"。
        """
        routed = make_router().route("/搜索transformer")

        assert isinstance(routed, UnknownCommand)
        assert routed.token == "搜索transformer"
        assert not isinstance(routed, Hit)

    def test_bare_slash_is_not_a_command(self) -> None:
        """光一个斜杠没有关键字可言，回"未知命令"没有提示价值。"""
        assert make_router().route("/") is None


class TestMentionForm:
    def test_alias_hit_keeps_the_whole_text_as_argument(self) -> None:
        hit = make_router().route("帮我搜索一下 transformer")

        assert hit.rule.keyword == "搜索"
        assert hit.matched is MatchKind.MENTION
        assert hit.trigger == "搜索"
        # 正文触发时内容是整条消息——用户没划出内容边界，我们也不该猜。
        assert hit.argument == "帮我搜索一下 transformer"

    def test_rule_without_aliases_never_fires_on_prose(self) -> None:
        """`摘要` 没配 aliases，所以正文里出现它也不该路由。"""
        assert make_router().route("帮我摘要一下这段") is None

    def test_alias_matching_is_case_insensitive_but_reports_the_original(self) -> None:
        router = make_router(Rule(keyword="search", aliases=("Transformer",)))

        assert router.route("transformers are great").trigger == "Transformer"

    def test_no_match_returns_none(self) -> None:
        assert make_router().route("今天天气不错") is None

    def test_blank_text_returns_none(self) -> None:
        assert make_router().route("   ") is None


class TestPrecedence:
    """三级 tiebreak：priority → 最长别名 → 声明顺序。"""

    def test_higher_priority_beats_a_longer_alias(self) -> None:
        router = make_router(
            Rule(keyword="摘要", aliases=("搜索并总结",)),
            Rule(keyword="搜索", aliases=("搜索",), priority=5),
        )

        assert router.route("搜索并总结").rule.keyword == "搜索"

    def test_longest_alias_breaks_priority_ties(self) -> None:
        router = make_router(
            Rule(keyword="摘要", aliases=("总结",)),
            Rule(keyword="搜索", aliases=("总结一下",)),
        )

        assert router.route("总结一下这个").rule.keyword == "搜索"

    def test_declaration_order_breaks_remaining_ties(self) -> None:
        router = make_router(
            Rule(keyword="搜索", aliases=("检索",)),
            Rule(keyword="摘要", aliases=("检索",)),
        )

        assert router.route("检索一下").rule.keyword == "搜索"

    def test_multiple_hits_are_logged_with_the_losers(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """只记"选中了谁"的话，事后没法知道当时还有哪些规则也匹配上了。"""
        router = make_router(
            Rule(keyword="搜索", aliases=("检索",)),
            Rule(keyword="摘要", aliases=("总结", "检索")),
        )

        with caplog.at_level(logging.INFO, logger=ROUTING_LOGGER):
            router.route("检索并总结")

        assert "命中 2 条规则" in caplog.text
        assert "搜索" in caplog.text
        assert "摘要" in caplog.text

    def test_unknown_command_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger=ROUTING_LOGGER):
            make_router().route("/sousuo x")

        assert "未知命令" in caplog.text


class TestRuleValidation:
    """坏规则要在启动时就炸，不是等第一条消息进来。"""

    def test_blank_keyword_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            Rule(keyword="  ")

    def test_keyword_with_a_slash_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            Rule(keyword="/搜索")

    def test_keyword_with_whitespace_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            Rule(keyword="搜 索")

    def test_blank_alias_is_rejected(self) -> None:
        """空别名会命中每一条消息。"""
        with pytest.raises(ValueError):
            Rule(keyword="搜索", aliases=("  ",))


class TestCollisions:
    def test_duplicate_keywords_are_rejected(self) -> None:
        rules = (Rule(keyword="搜索"), Rule(keyword="搜索"))

        with pytest.raises(ValueError, match="重复"):
            check_collisions(rules)

    def test_collision_is_case_insensitive(self) -> None:
        with pytest.raises(ValueError):
            check_collisions((Rule(keyword="search"), Rule(keyword="Search")))

    def test_distinct_keywords_pass(self) -> None:
        check_collisions((Rule(keyword="搜索"), Rule(keyword="摘要")))

    def test_builtin_help_keyword_cannot_be_reused(self) -> None:
        from feishu_bot.core.routing import BUILTIN_RULES

        with pytest.raises(ValueError):
            check_collisions((Rule(keyword=HELP_KEYWORD), *BUILTIN_RULES))


class TestHelp:
    def test_help_lists_every_keyword_with_its_usage(self) -> None:
        body = make_router().help_reply().body

        assert "/搜索 <内容>" in body
        assert "/摘要 <内容>" in body
        # requires_argument=False 的关键字不该写成「需要内容」。
        assert "/帮助" in body
        assert "/帮助 <内容>" not in body
        assert "在当前知识库里检索" in body

    def test_help_mentions_the_aliases(self) -> None:
        body = make_router().help_reply().body

        assert "查一下" in body
        assert "搜一下" in body

    def test_help_is_generated_from_the_injected_rules(self) -> None:
        """这条是"注册一个关键字帮助就自动更新"的核心证据。"""
        router = make_router(Rule(keyword="检索", description="自定义功能"))

        body = router.help_reply().body
        assert "/检索 <内容>" in body
        assert "自定义功能" in body
        assert "/搜索" not in body

    def test_help_has_exactly_one_line_per_rule(self) -> None:
        router = make_router(Rule(keyword="搜索"), Rule(keyword="找"))

        body = router.help_reply().body

        assert "/搜索" in body
        assert "/找" in body
        assert body.count("- ") == 2

    def test_help_mentions_the_unknown_command(self) -> None:
        assert "sousuo" in make_router().help_reply(unknown="sousuo").body

    def test_help_does_not_say_anything_about_unknown_by_default(self) -> None:
        assert "没有" not in make_router().help_reply().body


class TestUsage:
    def test_usage_reply_shows_the_keyword(self) -> None:
        rule = Rule(keyword="搜索", description="在当前知识库里检索")

        reply = make_router().usage_reply(rule)

        assert "/搜索 <内容>" in reply.body
        assert "在当前知识库里检索" in reply.body
        # 用户操作有误，橙色比蓝色更容易被看见。
        assert reply.tone is Tone.WARNING
