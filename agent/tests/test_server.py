"""HTTP 契约测试：不联网、不需要模型或 MCP。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from feishubot_agent.app.contracts import TurnReply, TurnRequest
from feishubot_agent.bootstrap import create_app
from feishubot_agent.settings import Settings


class _FakeService:
    def __init__(self) -> None:
        self.requests: list[TurnRequest] = []

    async def handle(self, request: TurnRequest) -> TurnReply:
        self.requests.append(request)
        return TurnReply(title="助手", text="收到", tone="success")


def test_healthz() -> None:
    app = create_app(Settings(), build_service=False)
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_turn_returns_503_before_service_is_wired() -> None:
    app = create_app(Settings(), build_service=False)
    with TestClient(app) as client:
        response = client.post(
            "/turn",
            json={
                "keyword": "助手",
                "text": "你好",
                "messageId": "om_1",
                "chatId": "oc_1",
            },
        )

    assert response.status_code == 503


def test_turn_accepts_backend_camel_case_payload() -> None:
    service = _FakeService()
    app = create_app(Settings(), service=service)

    with TestClient(app) as client:
        response = client.post(
            "/turn",
            json={
                "keyword": "助手",
                "text": "还剩几天年假？",
                "messageId": "om_1",
                "chatId": "oc_1",
                "senderId": "ou_1",
                "chatType": "group",
                "matched": "slash",
                "trigger": "助手",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"title": "助手", "text": "收到", "tone": "success"}
    assert service.requests[0].chat_id == "oc_1"
    assert service.requests[0].sender_id == "ou_1"
    assert service.requests[0].chat_type == "group"
