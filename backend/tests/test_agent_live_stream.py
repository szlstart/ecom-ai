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


def _publisher(
    redis: _Redis,
    *,
    user_no: str = "usr-test",
    conversation_no: str = "conv-test",
    store_id: int | None = None,
    run_no: str = "run-test",
) -> AgentLiveStreamPublisher:
    return AgentLiveStreamPublisher(
        redis,  # type: ignore[arg-type]
        Settings(environment="testing"),
        SimpleNamespace(
            conversation_no=conversation_no,
            store_id=store_id,
            conversation_type="store" if store_id is not None else "exclusive",
        ),  # type: ignore[arg-type]
        SimpleNamespace(user_no=user_no),  # type: ignore[arg-type]
        run_no,
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


@pytest.mark.asyncio
async def test_simultaneous_users_and_stores_publish_to_disjoint_realtime_channels() -> None:
    first_redis = _Redis()
    second_redis = _Redis()
    first = _publisher(
        first_redis,
        user_no="usr-a",
        conversation_no="conv-a",
        store_id=11,
        run_no="run-a",
    )
    second = _publisher(
        second_redis,
        user_no="usr-b",
        conversation_no="conv-b",
        store_id=22,
        run_no="run-b",
    )

    await first.publish("answer_replace", "用户 A 的回答")
    await second.publish("answer_replace", "用户 B 的回答")

    first_publications = first_redis.pipelines[-1].published
    second_publications = second_redis.pipelines[-1].published
    first_channels = {channel for channel, _payload in first_publications}
    second_channels = {channel for channel, _payload in second_publications}
    assert first_channels.isdisjoint(second_channels)
    assert all(
        "usr-b" not in payload and "conv-b" not in payload
        for _channel, payload in first_publications
    )
    assert all(
        "usr-a" not in payload and "conv-a" not in payload
        for _channel, payload in second_publications
    )
