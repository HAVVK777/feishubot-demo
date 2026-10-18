"""请假天数：从审批同步的多维表格读记录 + 纯函数求和。

两段刻意分开：

* `parse_records` / `sum_leave_days` 不碰网络、不读环境变量——给它们原始记录和区间就能
  算出结果，所以能直接单测。
* `get_leave_days` 负责编排：解析参数 → 读表 → 交给纯函数。

## 数据源

一张**由飞书审批同步过来的**多维表格（固定 `table_id`）。每行 = 一次审批，用到的列：

| 列 | 类型 | 用途 |
|---|---|---|
| 用户 | **人员** | 和 open_id 精确匹配 |
| 开始时间 | 日期 | 判断这条请假落在哪个日期 |
| 时长 | 数字 | 请假天数，**以 0.5 天为基础单位** |
| 审批结果 | 文本 | **只有值在「通过 / 已通过」里的行才计入**（可配） |

「结束时间」等其它列不用，放在表里无妨。

## 口径：按开始日期整条归属

一条 6/28–7/2、时长 5 天的请假，**只在查询区间包含 6/28 时计入**，算 5 天；只查 7 月时
不计入。

这不是计算精度问题，而是**数据口径**：审批同步表里只有总时长，没有"每个月各几天"。
按日历摊分在工作日口径下会算错（周末被摊进去），所以宁可用一个可解释的规则——整条归属
到开始日期——并在工具描述里写明。

## 只按 open_id 匹配

用户列**必须是人员字段**。人员列返回 `[{"id": "ou_…", "name": "…"}]`——`id` 就是
open_id，和 agent 从飞书事件里拿到的是同一个东西，所以能精确匹配。

**不接受文本列（姓名/英文名/工号）。** 文本列里没有 id，只能按姓名匹配，而重名会把
别人的假算进来——**这种错误不会报错**，只会给出一个看起来合理的天数。所以：

- `open_id` 不是 `ou_` 开头 → 直接拒绝。
- 用户列不是人员字段 → 直接拒绝，并说明要改成人员字段。
- 列名配错（找不到该列）→ 拒绝并列出实际列名。

一句话：**宁可明确失败，也不给一个可能错的数字。**

姓名从人员列自带的 `name` 取；取不到就显示 open_id。所以**不需要通讯录权限**。

## 配置

| 变量 | 说明 |
|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 必填，见 `feishu.py` |
| `FEISHU_LEAVE_BITABLE_APP_TOKEN` | 多维表格 `base` 后面那串 |
| `FEISHU_LEAVE_BITABLE_TABLE_ID` | 审批同步表在 URL 里 `table=` 后面那串 |
| `FEISHU_LEAVE_FIELD_USER` | 可选，默认 `用户` |
| `FEISHU_LEAVE_FIELD_TIME` | 可选，默认 `开始时间` |
| `FEISHU_LEAVE_FIELD_DAYS` | 可选，默认 `时长` |
| `FEISHU_LEAVE_FIELD_STATUS` | 可选，默认 `审批结果` |

应用还必须被加为这张多维表格的**文档应用**（不是"协作者"），否则接口会报 91403。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from feishubot_mcp.errors import ToolError
from feishubot_mcp.feishu import FeishuClient, FeishuError
from feishubot_mcp.tools.hints import hints

APP_TOKEN_ENV = "FEISHU_LEAVE_BITABLE_APP_TOKEN"
TABLE_ID_ENV = "FEISHU_LEAVE_BITABLE_TABLE_ID"
FIELD_USER_ENV = "FEISHU_LEAVE_FIELD_USER"
FIELD_TIME_ENV = "FEISHU_LEAVE_FIELD_TIME"
FIELD_DAYS_ENV = "FEISHU_LEAVE_FIELD_DAYS"
FIELD_STATUS_ENV = "FEISHU_LEAVE_FIELD_STATUS"
APPROVED_VALUES_ENV = "FEISHU_LEAVE_APPROVED_VALUES"

DEFAULT_FIELD_USER = "用户"
DEFAULT_FIELD_TIME = "开始时间"
DEFAULT_FIELD_DAYS = "时长"
DEFAULT_FIELD_STATUS = "审批结果"

#: 审批状态里这些值算"已通过"。飞书的审批同步表通常写「已通过」，自建表单可能写
#: 「通过」——两个都认，其它值（待审批、已撤销等）一律跳过。可用环境变量覆盖。
DEFAULT_APPROVED_VALUES = ("通过", "已通过")

#: 日期值里的年月日（文本列时用），形如 `2026-06-01`、`2026/6/1`、`2026年6月1日`。
_DATE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")

#: 文本里兜底取数字（比如 `1.5天`）。
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

#: 多维表格的日期列按北京时间解释——服务器通常是 UTC，直接用本地时区会偏一天。
SHANGHAI = ZoneInfo("Asia/Shanghai")

#: 懒建的客户端：导入时不读环境变量，所以测试和本地演示不需要凭据。
#: 测试里可以直接 monkeypatch 这个变量换成假的。
_client: FeishuClient | None = None


@dataclass(frozen=True, slots=True)
class LeaveEntry:
    """一条请假记录：从哪天开始、几天（0.5 的倍数）。"""

    start: date
    days: float


@hints(read_only=True)
async def get_leave_days(open_id: str, start_date: str, end_date: str) -> str:
    """查某人在指定时间段内请了几天假。用户问"某人这个月请假了吗/请了几天"时用它。

    `open_id` 必须是飞书的 **open_id**（`ou_` 开头，机器人事件里拿到的就是它）。日期写成
    YYYY-MM-DD，区间含两端。**跨出区间的请假按开始日期整条归属**：一条 6/28–7/2 的假，
    只有查询区间包含 6/28 时才计入。只统计审批结果为「通过」的记录。
    """
    open_id = open_id.strip()
    if not open_id.startswith("ou_"):
        raise ToolError(
            f"open_id 必须是 ou_ 开头的飞书 open_id，收到的是 {open_id!r}。"
            "这个工具只按 id 匹配，不支持姓名或工号——那会因为重名算错。"
        )

    start = _parse_argument_date(start_date, "开始日期")
    end = _parse_argument_date(end_date, "结束日期")
    if start > end:
        raise ToolError(f"开始日期（{start_date}）不能晚于结束日期（{end_date}）")

    try:
        raw = await _get_client().query_bitable_records(_app_token(), _table_id())
    except FeishuError as exc:
        # 上游失败是预料到的失败：让模型看到原因，由它决定怎么跟用户说。
        raise ToolError(f"读取请假表失败：{exc}") from exc

    field_user = _env(FIELD_USER_ENV, DEFAULT_FIELD_USER)
    field_time = _env(FIELD_TIME_ENV, DEFAULT_FIELD_TIME)
    field_days = _env(FIELD_DAYS_ENV, DEFAULT_FIELD_DAYS)
    field_status = _env(FIELD_STATUS_ENV, DEFAULT_FIELD_STATUS)

    _check_columns(
        raw,
        (
            (field_user, "用户列"),
            (field_time, "开始时间列"),
            (field_days, "时长列"),
            (field_status, "审批结果列"),
        ),
    )
    _check_user_column_is_person(raw, field_user)

    entries = parse_records(
        raw,
        open_id=open_id,
        field_user=field_user,
        field_time=field_time,
        field_days=field_days,
        field_status=field_status,
        approved=_approved_values(),
    )
    display = matched_person_name(raw, open_id=open_id, field_user=field_user) or open_id
    total = sum_leave_days(entries, start, end)

    if total <= 0:
        return f"{display} 在 {start_date} 至 {end_date} 没有请假记录。"
    return f"{display} 在 {start_date} 至 {end_date} 共请假 {total:g} 天。"


def parse_records(
    raw: list[dict[str, Any]],
    *,
    open_id: str,
    field_user: str = DEFAULT_FIELD_USER,
    field_time: str = DEFAULT_FIELD_TIME,
    field_days: str = DEFAULT_FIELD_DAYS,
    field_status: str = DEFAULT_FIELD_STATUS,
    approved: frozenset[str] = frozenset(DEFAULT_APPROVED_VALUES),
) -> list[LeaveEntry]:
    """筛出这个 open_id 的**已通过**记录，转成 `LeaveEntry`。

    只按人员列里的 id 精确匹配，不比较姓名——姓名会因重名把别人的假算进来，而且这种
    错误是静默的。列不是人员字段时由 `_check_user_column_is_person` 提前拒绝。

    认不出来的记录（缺列、日期/时长解析失败）**跳过**——一条脏数据不该让整次查询失败，
    但这也意味着表格里的脏数据不会被发现。维护表格时留意。
    """
    entries: list[LeaveEntry] = []
    for item in raw:
        fields = item.get("fields")
        if not isinstance(fields, dict):
            continue
        if open_id not in _person_ids(fields.get(field_user)):
            continue
        if _text_of(fields.get(field_status)).strip() not in approved:
            continue
        start = _parse_bitable_date(fields.get(field_time))
        days = _parse_days(fields.get(field_days))
        if start is None or days is None:
            continue
        entries.append(LeaveEntry(start=start, days=days))
    return entries


def sum_leave_days(entries: list[LeaveEntry], start: date, end: date) -> float:
    """区间内（含两端）的请假天数。

    **按开始日期整条归属**：记录的开始日期落在区间里就计入整条时长。跨出区间的部分
    不做摊分（见模块文档「口径」）。
    """
    return sum(entry.days for entry in entries if start <= entry.start <= end)


def matched_person_name(
    raw: list[dict[str, Any]], *, open_id: str, field_user: str = DEFAULT_FIELD_USER
) -> str | None:
    """表格人员列里这个 open_id 对应的姓名，找不到就是 None。

    人员列的行本身就带着 `{"id": "ou_…", "name": "…"}`——所以**不需要查通讯录**就能显示
    姓名。文本列（没有 id）这个工具根本不接受，所以通讯录在这里没有用武之地。
    """
    for item in raw:
        fields = item.get("fields")
        if not isinstance(fields, dict):
            continue
        for person in _objects(fields.get(field_user)):
            if person.get("id") != open_id:
                continue
            name = person.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _check_columns(raw: list[dict[str, Any]], wanted: tuple[tuple[str, str], ...]) -> None:
    """列名配错时**立刻报错**，别静默返回 0 天。

    列名拼错的表现和"这个人没请假"完全一样：一条都匹配不上、结果 0 天。所以只要表里
    有记录，就先核对列名在不在。
    """
    columns: set[str] = set()
    for item in raw:
        fields = item.get("fields")
        if isinstance(fields, dict):
            columns.update(str(name) for name in fields)
    if not columns:
        return  # 空表，没法核对

    missing = [label for column, label in wanted if column not in columns]
    if missing:
        raise ToolError(
            f"请假表里没有{'、'.join(missing)}对应的列（找的是 "
            f"{'、'.join(repr(c) for c, _ in wanted)}）。"
            f"实际列名：{'、'.join(sorted(columns))}。"
            "用 FEISHU_LEAVE_FIELD_USER / _TIME / _DAYS / _STATUS 指定正确的列名。"
        )


def _check_user_column_is_person(raw: list[dict[str, Any]], field_user: str) -> None:
    """用户列必须是**人员字段**，否则拒绝服务。

    文本列里没有 id，只能按姓名匹配——重名时会把别人的假算进来，而且**不会报错**。这种
    错误比"查不到"危险得多，所以宁可直接拒绝，也不给出一个看起来合理但可能错的天数。
    """
    for item in raw:
        fields = item.get("fields")
        if not isinstance(fields, dict):
            continue
        value = fields.get(field_user)
        if value in (None, "", []):
            continue
        if _person_ids(value):
            return  # 见到至少一个带 id 的值，确认是人员字段
        raise ToolError(
            f"用户列「{field_user}」不是人员字段，无法按 open_id 精确匹配。"
            "文本列只能按姓名匹配，重名会算错且不会报错——请把该列改成人员字段。"
        )


def _objects(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _person_ids(value: object) -> set[str]:
    """人员列里每个人的 open_id。"""
    return {
        raw.strip()
        for item in _objects(value)
        if isinstance(raw := item.get("id"), str) and raw.strip()
    }


def _parse_bitable_date(value: object) -> date | None:
    """多维表格的日期列返回**毫秒时间戳**；文本列返回字符串。两种都认。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, tz=SHANGHAI).date()
        except (OverflowError, OSError, ValueError):
            return None

    match = _DATE_RE.search(_text_of(value))
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _parse_days(value: object) -> float | None:
    """时长以 0.5 天为基础单位。数字直接取；文本先试 `float`，再兜底取第一个数字。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = _text_of(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    match = _NUMBER_RE.search(text)
    return float(match.group()) if match else None


def _text_of(value: object) -> str:
    """把文本列的分段数组拼成字符串；人员列取姓名。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        raw = value.get("text") or value.get("name")
        return raw if isinstance(raw, str) else ""
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                raw = item.get("text")
                if not isinstance(raw, str):
                    raw = item.get("name")
                if isinstance(raw, str):
                    parts.append(raw)
        return "".join(parts)
    return ""


def _parse_argument_date(raw: str, label: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ToolError(f"{label}格式不对：{raw!r}，要写成 YYYY-MM-DD（例如 2026-06-07）") from exc


def _app_token() -> str:
    value = os.environ.get(APP_TOKEN_ENV, "").strip()
    if not value:
        raise FeishuError(f"未配置 {APP_TOKEN_ENV}（多维表格 URL 里 base 后面那串）")
    return value


def _table_id() -> str:
    value = os.environ.get(TABLE_ID_ENV, "").strip()
    if not value:
        raise FeishuError(f"未配置 {TABLE_ID_ENV}（表格 URL 里 table= 后面那串）")
    return value


def _approved_values() -> frozenset[str]:
    raw = os.environ.get(APPROVED_VALUES_ENV, "")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    return frozenset(values) if values else frozenset(DEFAULT_APPROVED_VALUES)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _get_client() -> FeishuClient:
    global _client
    if _client is None:
        _client = FeishuClient.from_environment()
    return _client


#: 这个模块对外提供的工具。函数名即工具名，重名会在启动时直接报错。
TOOLS = (get_leave_days,)
