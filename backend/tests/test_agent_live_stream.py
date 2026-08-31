from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from redis.exceptions import RedisError

from app.core.config import Settings
from app.modules.agent_runtime.live_stream import AgentLiveStreamPublisher


class _Pipeline:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[tuple[str, str]] = []

    def publish(self, channel: str, payload: str) -> None:
        self.published.append((channel, payload))

    async def execute(self) -> list[int]:
        if self.fail:
            raise RedisError("redis unavailable")
        return [1 for _ in self.published]


class _Redis:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.pipelines: list[_Pipeline] = []

    def pipeline(self, *, transaction: bool) -> _Pipeline:
        assert transaction is False
        pipeline = _Pipeline(fail=self.fail)
        self.pipelines.append(pipeline)
        return pipeline


def _publisher(redis: _Redis) -> AgentLiveStreamPublisher:
    return AgentLiveStreamPublisher(
        redis,  # type: ignore[arg-type]
        Settings(environment="testing"),
        SimpleNamespace(
            conversation_no="conv-test",
            store_id=None,
            conversation_type="exclusive",
        ),  # type: ignore[arg-type]
        SimpleNamespace(user_no="usr-test"),  # type: ignore[arg-type]
        "run-test",
    )


@pytest.mark.asyncio
async def test_answer_replace_can_shrink_a_previously_streamed_answer() -> None:
    redis = _Redis()
    publisher = _publisher(redis)

    await publisher.publish("answer", "unsafe draft")
    await publisher.publish("answer_replace", "安全回复")

    assert publisher.answer_index == 2
    replacement = json.loads(redis.pipelines[-1].published[0][1])
    assert replacement["data"]["chunk_index"] == 2
    assert replacement["data"]["text_so_far"] == "安全回复"


@pytest.mark.asyncio
async def test_redis_stream_failure_does_not_abort_durable_agent_reply() -> None:
    publisher = _publisher(_Redis(fail=True))

    await publisher.publish("reasoning", "正在核对授权数据")
    await publisher.publish("answer_replace", "稍后从持久化消息恢复")

    assert publisher.reasoning_index == 1
    assert publisher.answer_index == 1
