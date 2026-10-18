"""请假天数：审批同步表的解析 + 纯计算 + 工具编排。

口径：**只统计已通过**，且**按开始日期整条归属**（不跨区间摊分）。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from feishubot_mcp.errors import ToolError
from feishubot_mcp.feishu import FeishuError
from feishubot_mcp.tools import leave
from feishubot_mcp.tools.leave import SHANGHAI, LeaveEntry

OPEN_ID = "ou_x"


def stamp(year: int, month: int, day: int) -> float:
    """多维表格日期列的样子：毫秒时间戳。"""
    return datetime(year, month, day, tzinfo=SHANGHAI).timestamp() * 1000


def record(
    *,
    user: str = OPEN_ID,
    start: object = None,
    days: object = 1.0,
    user_field: object = None,
    status: object = "通过",
) -> dict:
    return {
        "fields": {
            "用户": user_field if user_field is not None else [{"id": user, "name": "张三"}],
            "开始时间": start if start is not None else stamp(2024, 10, 2),
            "时长": days,
            "审批结果": status,
        }
    }


# --------------------------------------------------------------------- 解析


@pytest.mark.parametrize("status", ["待审批", "已撤销", "", None])
def test_only_approved_records_count(status):
    """审批状态不是「通过 / 已通过」的行一律跳过。"""
    raw = [record(status=status), record(status=[{"text": "通过"}])]
    assert len(leave.parse_records(raw, open_id=OPEN_ID)) == 1


@pytest.mark.parametrize("status", ["通过", "已通过", [{"text": "已通过"}]])
def test_both_approved_spellings_are_accepted(status):
    """飞书审批同步表写「已通过」，自建表单常写「通过」。"""
    assert len(leave.parse_records([record(status=status)], open_id=OPEN_ID)) == 1


def test_matches_the_open_id_in_a_person_column():
    assert len(leave.parse_records([record()], open_id=OPEN_ID)) == 1


def test_same_name_different_id_does_not_match():
    """重名的人不会串——比的是 id，不是姓名。"""
    other = record(user_field=[{"id": "ou_y", "name": "张三"}])
    assert leave.parse_records([other], open_id=OPEN_ID) == []


def test_same_day_multiple_records_are_all_counted():
    """审批同步表里一行就是一次审批，同一天两条都要算（不按日期去重）。"""
    raw = [record(days=0.5), record(days=0.5)]
    assert sum(e.days for e in leave.parse_records(raw, open_id=OPEN_ID)) == 1.0


def test_skips_records_with_unparseable_dates_or_days():
    raw = [
        record(start="不是日期"),
        record(start=None, days=None),
        record(days="不是数字"),
        record(),  # 唯一一条好的
    ]
    assert len(leave.parse_records(raw, open_id=OPEN_ID)) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (stamp(2024, 10, 3), date(2024, 10, 3)),  # 日期列：毫秒时间戳
        ("2024-10-03", date(2024, 10, 3)),  # 文本列
        ("2024/10/3", date(2024, 10, 3)),
        ("2024年10月3日", date(2024, 10, 3)),
        ("", None),
        (None, None),
        ("2024-13-40", None),
    ],
)
def test_date_parsing(value, expected):
    assert leave._parse_bitable_date(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, 1.0),
        (0.5, 0.5),  # 0.5 天是基础单位
        (2.5, 2.5),
        ("1.5", 1.5),
        ("1.5天", 1.5),  # 文本列带单位也能取出来
        ("", None),
        (None, None),
        ("不是数字", None),
    ],
)
def test_days_parsing(value, expected):
    assert leave._parse_days(value) == expected


# --------------------------------------------------------------------- 计算


def test_sums_days_in_range_inclusive():
    entries = [
        LeaveEntry(date(2024, 10, 1), 1.0),
        LeaveEntry(date(2024, 10, 3), 0.5),
        LeaveEntry(date(2024, 10, 31), 2.0),
    ]
    assert leave.sum_leave_days(entries, date(2024, 10, 1), date(2024, 10, 31)) == 3.5


def test_excludes_entries_outside_the_range():
    entries = [LeaveEntry(date(2024, 9, 30), 1.0), LeaveEntry(date(2024, 11, 1), 1.0)]
    assert leave.sum_leave_days(entries, date(2024, 10, 1), date(2024, 10, 31)) == 0


def test_a_cross_month_leave_is_attributed_to_its_start_date():
    """6/28–7/2 的 5 天假：查 6 月算 5 天，查 7 月算 0 天。"""
    entries = [LeaveEntry(date(2024, 6, 28), 5.0)]

    assert leave.sum_leave_days(entries, date(2024, 6, 1), date(2024, 6, 30)) == 5.0
    assert leave.sum_leave_days(entries, date(2024, 7, 1), date(2024, 7, 31)) == 0.0


# --------------------------------------------------------------------- 工具


class FakeClient:
    def __init__(self, records: list[dict] | None = None, error: Exception | None = None) -> None:
        self._records = records or []
        self._error = error
        self.calls: list[tuple[str, str]] = []

    async def query_bitable_records(self, app_token: str, table_id: str):
        self.calls.append((app_token, table_id))
        if self._error is not None:
            raise self._error
        return self._records


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    monkeypatch.setenv("FEISHU_LEAVE_BITABLE_APP_TOKEN", "app-token")
    monkeypatch.setenv("FEISHU_LEAVE_BITABLE_TABLE_ID", "tbl-1")
    fake = FakeClient([record(start=stamp(2024, 10, 2)), record(start=stamp(2024, 10, 3))])
    monkeypatch.setattr(leave, "_client", fake)
    return fake


async def test_tool_sums_the_range(configured: FakeClient):
    text = await leave.get_leave_days(OPEN_ID, "2024-10-01", "2024-10-31")

    assert "共请假 2 天" in text
    assert configured.calls == [("app-token", "tbl-1")]


async def test_tool_replies_with_the_name_from_the_person_column(configured: FakeClient):
    text = await leave.get_leave_days(OPEN_ID, "2024-10-01", "2024-10-31")

    assert text.startswith("张三 在")


async def test_tool_only_counts_starts_inside_the_range(configured: FakeClient):
    text = await leave.get_leave_days(OPEN_ID, "2024-10-02", "2024-10-02")

    assert "共请假 1 天" in text


async def test_tool_says_so_when_there_is_nothing(
    monkeypatch: pytest.MonkeyPatch, configured: FakeClient
):
    configured._records = []
    text = await leave.get_leave_days(OPEN_ID, "2024-10-01", "2024-10-31")

    assert "没有请假记录" in text


async def test_tool_rejects_a_non_open_id(configured: FakeClient):
    with pytest.raises(ToolError, match="必须是 ou_ 开头的飞书 open_id"):
        await leave.get_leave_days("张三", "2024-10-01", "2024-10-31")

    assert configured.calls == []


async def test_tool_rejects_a_text_user_column(monkeypatch: pytest.MonkeyPatch):
    """用户列是文本列 → 明确拒绝，而不是按姓名匹配。"""
    monkeypatch.setenv("FEISHU_LEAVE_BITABLE_APP_TOKEN", "app-token")
    monkeypatch.setenv("FEISHU_LEAVE_BITABLE_TABLE_ID", "tbl-1")
    monkeypatch.setattr(leave, "_client", FakeClient([record(user_field=[{"text": "张三"}])]))

    with pytest.raises(ToolError, match="不是人员字段"):
        await leave.get_leave_days(OPEN_ID, "2024-10-01", "2024-10-31")


async def test_wrong_column_name_fails_loudly(monkeypatch: pytest.MonkeyPatch):
    """列名配错和"没请假"表现一样（0 天）——必须报错，否则是静默错误答案。"""
    monkeypatch.setenv("FEISHU_LEAVE_BITABLE_APP_TOKEN", "app-token")
    monkeypatch.setenv("FEISHU_LEAVE_BITABLE_TABLE_ID", "tbl-1")
    monkeypatch.setenv("FEISHU_LEAVE_FIELD_DAYS", "不存在的列")
    monkeypatch.setattr(leave, "_client", FakeClient([record()]))

    with pytest.raises(ToolError, match="没有时长列"):
        await leave.get_leave_days(OPEN_ID, "2024-10-01", "2024-10-31")


async def test_missing_status_column_fails_loudly(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FEISHU_LEAVE_BITABLE_APP_TOKEN", "app-token")
    monkeypatch.setenv("FEISHU_LEAVE_BITABLE_TABLE_ID", "tbl-1")
    incomplete = {
        "fields": {
            "用户": [{"id": OPEN_ID, "name": "张三"}],
            "开始时间": stamp(2024, 10, 2),
            "时长": 1.0,
        }
    }
    monkeypatch.setattr(leave, "_client", FakeClient([incomplete]))

    with pytest.raises(ToolError, match="没有审批结果列"):
        await leave.get_leave_days(OPEN_ID, "2024-10-01", "2024-10-31")


async def test_bad_date_format_is_a_tool_error(configured: FakeClient):
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await leave.get_leave_days(OPEN_ID, "2024/10/01", "2024-10-31")


async def test_reversed_range_is_a_tool_error(configured: FakeClient):
    with pytest.raises(ToolError, match="不能晚于"):
        await leave.get_leave_days(OPEN_ID, "2024-10-31", "2024-10-01")


async def test_upstream_failure_becomes_a_tool_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FEISHU_LEAVE_BITABLE_APP_TOKEN", "app-token")
    monkeypatch.setenv("FEISHU_LEAVE_BITABLE_TABLE_ID", "tbl-1")
    monkeypatch.setattr(leave, "_client", FakeClient(error=FeishuError("表格没授权")))

    with pytest.raises(ToolError, match="读取请假表失败"):
        await leave.get_leave_days(OPEN_ID, "2024-10-01", "2024-10-31")


async def test_missing_table_config_is_a_tool_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FEISHU_LEAVE_BITABLE_APP_TOKEN", "app-token")
    monkeypatch.delenv("FEISHU_LEAVE_BITABLE_TABLE_ID", raising=False)
    monkeypatch.setattr(leave, "_client", FakeClient())

    with pytest.raises(ToolError, match="FEISHU_LEAVE_BITABLE_TABLE_ID"):
        await leave.get_leave_days(OPEN_ID, "2024-10-01", "2024-10-31")


def test_tool_is_declared_read_only():
    from feishubot_mcp.tools.hints import annotations_of

    declared = annotations_of(leave.get_leave_days)
    assert declared is not None
    assert declared.read_only_hint is True
