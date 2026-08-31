from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.id_generator import new_prefixed_ulid
from app.core.security import utc_now
from app.modules.identity.models import User
from app.modules.messaging.models import Conversation
from app.modules.realtime.channels import admin_platform_channel, admin_store_channel, user_channel

AgentStreamCallback = Callable[[str, str], Awaitable[None]]


class AgentLiveStreamPublisher:
    """Best-effort low-latency provider stream over the existing realtime channels.

    Final messages and completion events still use the durable MySQL Outbox. These frames are
    deliberately ephemeral: a reconnect recovers the persisted final answer through REST.
    """

    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        conversation: Conversation,
        user: User,
        run_id: str,
    ) -> None:
        self.redis = redis
        self.settings = settings
        self.conversation = conversation
        self.user = user
        self.run_id = run_id
        self.reasoning_index = 0
        self.answer_index = 0
        self.last_answer_length = 0

    async def publish(self, kind: str, text_so_far: str) -> None:
        if kind in {"reasoning", "reasoning_replace"}:
            self.reasoning_index += 1
            event_type = "agent.response.reasoning.delta"
            chunk_index = self.reasoning_index
            text = text_so_far[:6000]
        elif kind in {"answer", "answer_replace"}:
            # Providers commonly emit one or two characters per delta. Coalesce those
            # micro-deltas before Redis while preserving cumulative text semantics.
            if kind == "answer" and len(text_so_far) - self.last_answer_length < 6:
                return
            self.last_answer_length = len(text_so_far)
            self.answer_index += 1
            event_type = "agent.response.delta"
            chunk_index = self.answer_index
            text = text_so_far[:4000]
        else:
            return
        frame = {
            "schema_version": 1,
            "event_id": new_prefixed_ulid("rte_"),
            "type": event_type,
            "occurred_at": utc_now().isoformat() + "Z",
            "data": {
                "conversation_id": self.conversation.conversation_no,
                "run_id": self.run_id,
                "chunk_index": chunk_index,
                "text_so_far": text,
            },
        }
        channels = [user_channel(self.settings.environment, self.user.user_no)]
        if self.conversation.store_id is not None:
            channels.append(
                admin_store_channel(self.settings.environment, self.conversation.store_id)
            )
        elif self.conversation.conversation_type == "exclusive":
            channels.append(admin_platform_channel(self.settings.environment))
        try:
            payload = json.dumps(frame, separators=(",", ":"), ensure_ascii=False)
            pipeline = self.redis.pipeline(transaction=False)
            for channel in dict.fromkeys(channels):
                pipeline.publish(channel, payload)
            await pipeline.execute()
        except RedisError:
            # Realtime delivery is an enhancement. The durable final message remains authoritative.
            return
