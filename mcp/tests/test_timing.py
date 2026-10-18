"""执行时间元数据的换算。"""

from __future__ import annotations

from feishubot_mcp.timing import EXECUTION_TIME_META_KEY, execution_time_meta


def test_seconds_are_converted_to_milliseconds() -> None:
    assert execution_time_meta(0.12345) == {EXECUTION_TIME_META_KEY: 123.45}


def test_rounding_keeps_microsecond_precision() -> None:
    assert execution_time_meta(0.000123456) == {EXECUTION_TIME_META_KEY: 0.123}
