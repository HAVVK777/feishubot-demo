"""飞书客户端：token 缓存、分片、错误归一。用 MockTransport，不联网。"""

from __future__ import annotations

import json
from datetime import date

import httpx2
import pytest

from feishubot_mcp.feishu import (
    LEAVE_APPROVALS_PATH,
    TOKEN_PATH,
    FeishuClient,
    FeishuError,
)

APP_ID = "cli_test"
APP_SECRET = "secret"


class Recorder:
    """记录请求、按路径回包。"""

    def __init__(
        self,
        *,
        token_response: dict | None = None,
        query_response: dict | None = None,
        query_status: int = 200,
    ) -> None:
        self.requests: list[httpx2.Request] = []
        self._token_response = token_response or {
            "code": 0,
            "tenant_access_token": "t-1",
            "expire": 7200,
        }
        self._query_response = query_response or {"code": 0, "data": {"user_approvals": []}}
        self._query_status = query_status

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path == TOKEN_PATH:
            return httpx2.Response(200, json=self._token_response)
        return httpx2.Response(self._query_status, json=self._query_response)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def queries(self) -> list[dict]:
        return [
            json.loads(request.content)
            for request in self.requests
            if request.url.path == LEAVE_APPROVALS_PATH
        ]


def make_client(recorder: Recorder) -> FeishuClient:
    return FeishuClient(APP_ID, APP_SECRET, transport=httpx2.MockTransport(recorder))


async def test_token_is_fetched_once_and_reused():
    recorder = Recorder()
    client = make_client(recorder)

    await client.query_leave_approvals("ou_x", date(2024, 1, 1), date(2024, 1, 2))
    await client.query_leave_approvals("ou_x", date(2024, 1, 1), date(2024, 1, 2))

    assert recorder.paths().count(TOKEN_PATH) == 1
    await client.aclose()


async def test_requests_carry_the_bearer_token():
    recorder = Recorder()
    client = make_client(recorder)

    await client.query_leave_approvals("ou_x", date(2024, 1, 1), date(2024, 1, 2))

    query = next(r for r in recorder.requests if r.url.path == LEAVE_APPROVALS_PATH)
    assert query.headers["authorization"] == "Bearer t-1"
    await client.aclose()


async def test_long_ranges_are_split_into_30_day_windows():
    """假勤接口一次最多 30 天——45 天要分两次，且窗口不能重叠、不能漏。"""
    recorder = Recorder()
    client = make_client(recorder)

    await client.query_leave_approvals("ou_x", date(2024, 1, 1), date(2024, 2, 14))

    queries = recorder.queries()
    assert [q["check_date_from"] for q in queries] == ["20240101", "20240131"]
    assert [q["check_date_to"] for q in queries] == ["20240130", "20240214"]
    await client.aclose()


async def test_results_from_all_chunks_are_concatenated():
    recorder = Recorder(
        query_response={"code": 0, "data": {"user_approvals": [{"date": "20240102"}]}}
    )
    client = make_client(recorder)

    records = await client.query_leave_approvals("ou_x", date(2024, 1, 1), date(2024, 2, 14))

    assert records == [{"date": "20240102"}, {"date": "20240102"}]
    await client.aclose()


async def test_business_error_inside_http_200_is_raised():
    """飞书把业务错误包在 200 里——只看状态码会把失败当成功。"""
    recorder = Recorder(query_response={"code": 99991663, "msg": "invalid token"})
    client = make_client(recorder)

    with pytest.raises(FeishuError, match="code=99991663"):
        await client.query_leave_approvals("ou_x", date(2024, 1, 1), date(2024, 1, 2))
    await client.aclose()


async def test_http_failure_is_raised():
    recorder = Recorder(query_response={"msg": "boom"}, query_status=500)
    client = make_client(recorder)

    with pytest.raises(FeishuError, match="HTTP 500"):
        await client.query_leave_approvals("ou_x", date(2024, 1, 1), date(2024, 1, 2))
    await client.aclose()


async def test_missing_token_in_response_is_raised():
    recorder = Recorder(token_response={"code": 0})
    client = make_client(recorder)

    with pytest.raises(FeishuError, match="没有 tenant_access_token"):
        await client.query_leave_approvals("ou_x", date(2024, 1, 1), date(2024, 1, 2))
    await client.aclose()


def test_missing_credentials_fail_with_a_clear_message(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    with pytest.raises(FeishuError, match="未配置"):
        FeishuClient.from_environment()


async def test_bitable_records_are_paginated():
    """一张表读全量：按 page_token 翻页，拼起来。"""
    pages = [
        {
            "code": 0,
            "data": {"items": [{"record_id": "r1"}], "has_more": True, "page_token": "p2"},
        },
        {"code": 0, "data": {"items": [{"record_id": "r2"}], "has_more": False}},
    ]
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == TOKEN_PATH:
            return httpx2.Response(
                200, json={"code": 0, "tenant_access_token": "t-1", "expire": 7200}
            )
        seen.append(request)
        return httpx2.Response(200, json=pages[len(seen) - 1])

    client = FeishuClient(APP_ID, APP_SECRET, transport=httpx2.MockTransport(handler))

    records = await client.query_bitable_records("app-x", "tbl-1")

    assert [r["record_id"] for r in records] == ["r1", "r2"]
    assert seen[0].url.path == "/open-apis/bitable/v1/apps/app-x/tables/tbl-1/records"
    assert dict(seen[0].url.params)["page_size"] == "500"
    assert "page_token" not in seen[0].url.params
    assert dict(seen[1].url.params)["page_token"] == "p2"
    await client.aclose()


async def test_bitable_permission_error_is_raised():
    """应用没被加为表格协作者时，报的是业务错误——别当成 token 问题。"""
    recorder = Recorder(query_response={"code": 1254302, "msg": "no permission"})
    client = make_client(recorder)

    with pytest.raises(FeishuError, match="1254302"):
        await client.query_bitable_records("app-x", "tbl-1")
    await client.aclose()
