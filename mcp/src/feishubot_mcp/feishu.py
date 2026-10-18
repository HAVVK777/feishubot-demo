"""飞书开放平台的最小客户端：换 tenant_access_token、查假勤审批数据。

不是 MCP 协议的一部分——它是**工具的外部依赖**。放在顶层而不是 `tools/` 里，是因为
token 缓存应该被所有飞书工具共用：每次调用都去换一次 token，等于白白消耗调用额度。

配置从环境变量读（工具自己的配置由工具自己读，不进 `settings.py`）：

| 变量 | 说明 |
|---|---|
| `FEISHU_APP_ID` | 自建应用的 App ID |
| `FEISHU_APP_SECRET` | 自建应用的 App Secret |
| `FEISHU_BASE_URL` | 可选，默认 `https://open.feishu.cn`（测试指向假服务） |

## 两个必须记住的飞书坑

1. **业务错误包在 HTTP 200 里**，错误码在 body 的 `code` 字段。只看状态码会把失败当成功。
2. **假勤审批接口一次最多查 30 天**，超过要分片——`query_leave_approvals` 已经处理了。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import httpx2

APP_ID_ENV = "FEISHU_APP_ID"
APP_SECRET_ENV = "FEISHU_APP_SECRET"
BASE_URL_ENV = "FEISHU_BASE_URL"

DEFAULT_BASE_URL = "https://open.feishu.cn"

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
LEAVE_APPROVALS_PATH = "/open-apis/attendance/v1/user_approvals/query"
BITABLE_RECORDS_PATH = "/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"

#: 假勤审批查询一次最多覆盖多少天（官方限制，超过会被拒）。
MAX_QUERY_DAYS = 30

#: 多维表格记录一页最多 500 条。
BITABLE_PAGE_SIZE = 500

#: 分页上限，防止响应异常时死循环（50 页 = 2.5 万条，远超一张月度表的分量）。
BITABLE_MAX_PAGES = 50

#: 提前多久换 token。`expire` 是秒；提前换掉，免得在边界上撞到过期。
TOKEN_REFRESH_MARGIN_SECONDS = 300


class FeishuError(RuntimeError):
    """飞书接口调用失败：连不上、HTTP 错误、业务 code 非 0、凭据没配。

    工具应该把它翻成 `ToolError`——这是**预料到**的失败（上游不可用/参数不对），
    不是 bug，模型和用户都该看到原因。
    """


class FeishuClient:
    """一个飞书自建应用的最小客户端。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._http = httpx2.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=10.0,
            transport=transport,
        )
        self._token = ""
        self._token_expires_at = 0.0
        #: 双检锁：并发调用同时发现 token 过期时，只换一次。
        self._lock = asyncio.Lock()

    @classmethod
    def from_environment(cls) -> FeishuClient:
        app_id = os.environ.get(APP_ID_ENV, "").strip()
        app_secret = os.environ.get(APP_SECRET_ENV, "").strip()
        if not app_id or not app_secret:
            raise FeishuError(
                f"未配置 {APP_ID_ENV} / {APP_SECRET_ENV}（飞书后台「凭证与基础信息」里获取）"
            )
        return cls(
            app_id,
            app_secret,
            base_url=os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def query_leave_approvals(
        self,
        user_id: str,
        start: date,
        end: date,
        *,
        employee_type: str = "open_id",
    ) -> list[dict[str, Any]]:
        """查 [start, end] 内的假勤审批记录（原始 JSON，未解析）。

        `employee_type` 决定 `user_id` 是什么：`open_id`（`ou_…`，飞书事件里拿到的就是
        它）、`employee_id` 或 `employee_no`。**以飞书后台该接口的说明为准**——不同租户
        开通的字段可能不同。

        超过 30 天会自动分片，按时间顺序拼接结果。
        """
        token = await self._tenant_token()
        records: list[dict[str, Any]] = []
        for chunk_start, chunk_end in _chunks(start, end):
            payload = await self._request(
                "POST",
                LEAVE_APPROVALS_PATH,
                body={
                    "user_ids": [user_id],
                    "check_date_from": chunk_start.strftime("%Y%m%d"),
                    "check_date_to": chunk_end.strftime("%Y%m%d"),
                    "employee_type": employee_type,
                },
                token=token,
            )
            data = payload.get("data") or {}
            records.extend(data.get("user_approvals") or [])
        return records

    async def query_bitable_records(self, app_token: str, table_id: str) -> list[dict[str, Any]]:
        """读一张多维表格的全部记录（自动翻页）。

        返回的是原始记录：`[{"record_id": ..., "fields": {...}}, ...]`。字段值的形状取决于
        列类型（人员字段是对象数组、日期字段是毫秒时间戳、文本字段是分段数组）——解析在
        调用方做，因为只有它知道自己的列名和列类型。

        **权限**：应用必须被加为这张多维表格的协作者，否则会返回权限错误——那个错误看起来
        很像 token 问题，注意区分。
        """
        token = await self._tenant_token()
        path = BITABLE_RECORDS_PATH.format(app_token=app_token, table_id=table_id)
        records: list[dict[str, Any]] = []
        page_token = ""

        for _ in range(BITABLE_MAX_PAGES):
            params: dict[str, Any] = {"page_size": BITABLE_PAGE_SIZE}
            if page_token:
                params["page_token"] = page_token
            payload = await self._request("GET", path, params=params, token=token)
            data = payload.get("data") or {}
            records.extend(data.get("items") or [])
            page_token = data.get("page_token") or ""
            if not data.get("has_more") or not page_token:
                break
        return records

    async def _tenant_token(self) -> str:
        """取 tenant_access_token，缓存到快过期为止。"""
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token

        async with self._lock:
            # 双重检查：等锁的时候别人可能已经换好了。
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token

            payload = await self._request(
                "POST",
                TOKEN_PATH,
                body={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            token = payload.get("tenant_access_token")
            if not isinstance(token, str) or not token:
                raise FeishuError("换 tenant_access_token 的响应里没有 tenant_access_token")
            expire = payload.get("expire")
            lifetime = float(expire) if isinstance(expire, (int, float)) else 7200.0
            self._token = token
            self._token_expires_at = time.monotonic() + max(
                60.0, lifetime - TOKEN_REFRESH_MARGIN_SECONDS
            )
            return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        """请求一个飞书接口，把三种失败归一成 `FeishuError`。"""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = await self._http.request(
                method, path, json=body, params=params, headers=headers
            )
        except httpx2.HTTPError as exc:
            raise FeishuError(f"请求 {path} 失败：{exc}") from exc

        if response.status_code != 200:
            raise FeishuError(f"{path} 返回 HTTP {response.status_code}：{response.text[:200]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise FeishuError(f"{path} 的响应不是 JSON：{response.text[:200]}") from exc

        if not isinstance(payload, dict):
            raise FeishuError(f"{path} 的响应不是对象：{payload!r}")

        # 飞书把业务错误也放在 HTTP 200 里——这一行才是真正的成败判断。
        code = payload.get("code")
        if code != 0:
            raise FeishuError(f"{path} 业务错误：code={code} msg={payload.get('msg')!r}")
        return payload


def _chunks(start: date, end: date) -> Iterator[tuple[date, date]]:
    """把区间切成不超过 30 天的窗口（含两端）。"""
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=MAX_QUERY_DAYS - 1), end)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)
