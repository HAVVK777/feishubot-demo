"""harness 的统一错误类型。

分工和 MCP 服务的错误模型一致：

* **预料到的失败**用具体异常并给出可判断的 `kind`（能不能重试、要不要告诉用户）；
* **没预料到的崩溃**让它抛，由服务层记日志并转成兜底文案。

`timeout` 永远不自动重试：超时只说明「不知道做没做」，重发可能把一次操作变成两次。
"""

from __future__ import annotations


class HarnessError(RuntimeError):
    """harness 内所有可预期错误的基类。"""


class ModelError(HarnessError):
    """模型调用失败（凭据、限流、网络、响应格式）。"""


class ToolCallError(HarnessError):
    """工具调用失败。

    `kind` 是给**代码**看的，不靠解析中文消息来决定重试：

    * `rate_limited`：调用太快，可以按退避重试；
    * `connection`：连接失败，可以有界重试；
    * `timeout`：不知道工具是否已经执行，**不要自动重试**；
    * `unauthorized` / `missing_meta`：调用方问题，重试没有意义；
    * `unknown`：按不可重试处理。
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        retryable: bool = False,
        tool: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.tool = tool
        #: 服务端给出了明确的等待时间时用它覆盖默认退避；没有就用指数退避。
        self.retry_after_seconds = retry_after_seconds


class BudgetExceeded(HarnessError):
    """回合超过了步数或时间预算。"""


class SessionBusy(HarnessError):
    """同一会话的上一条消息还在处理，等待也超时了。"""
