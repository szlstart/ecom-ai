from __future__ import annotations

import os
import secrets

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.config import get_settings
from app.main import create_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("ECOM_RUN_INTEGRATION_TESTS") != "1",
        reason="set ECOM_RUN_INTEGRATION_TESTS=1 with isolated MySQL and Redis services",
    ),
]


def test_websocket_uses_origin_checked_one_time_subprotocol_ticket() -> None:
    get_settings.cache_clear()
    suffix = secrets.token_hex(5)
    with TestClient(create_app(), base_url="http://testserver") as client:
        config = client.get("/api/v1/auth/registration-config").json()["data"]
        captcha = config["captcha"]
        left, operator, right, _, _ = captcha["question"].split()
        captcha_answer = int(left) + int(right) if operator == "+" else int(left) - int(right)
        email = f"realtime_{suffix}@example.com"
        registration = client.post(
            "/api/v1/auth/registrations",
            headers={"Idempotency-Key": f"realtime-registration-{suffix}"},
            json={
                "username": f"realtime_{suffix}",
                "email": email,
                "captcha_id": captcha["captcha_id"],
                "captcha_answer": str(captcha_answer),
                "password": f"Correct-Horse-{suffix}-Battery-Staple!",
                "config_version": config["config_version"],
                "agreement_acceptances": [
                    {
                        "document_type": item["document_type"],
                        "document_version": item["document_version"],
                    }
                    for item in config["required_agreements"]
                ],
                "locale": "zh-CN",
                "timezone": "Asia/Shanghai",
            },
        )
        assert registration.status_code == 201, registration.text
        token = registration.json()["data"]["access_token"]
        ticket_response = client.post(
            "/api/v1/realtime/tickets", headers={"Authorization": f"Bearer {token}"}
        )
        assert ticket_response.status_code == 200
        ticket = ticket_response.json()["data"]["ticket"]
        subprotocols = ["ecom.realtime.v1", f"ticket.{ticket}"]

        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "/ws/v1",
                headers={"origin": "https://attacker.example"},
                subprotocols=subprotocols,
            ):
                pass
        assert rejected.value.code == 4403

        with client.websocket_connect(
            "/ws/v1",
            headers={"origin": "http://127.0.0.1:5173"},
            subprotocols=subprotocols,
        ) as websocket:
            assert websocket.accepted_subprotocol == "ecom.realtime.v1"
            ready = websocket.receive_json()
            assert ready["type"] == "connection.ready"
            assert ready["data"]["audience"] == "user"
            websocket.send_json({"type": "subscribe", "conversation_id": "conv_foreign"})
            with pytest.raises(WebSocketDisconnect) as invalid_frame:
                websocket.receive_json()
            assert invalid_frame.value.code == 1008

        with pytest.raises(WebSocketDisconnect) as replayed:
            with client.websocket_connect(
                "/ws/v1",
                headers={"origin": "http://127.0.0.1:5173"},
                subprotocols=subprotocols,
            ):
                pass
        assert replayed.value.code == 4401
