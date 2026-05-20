"""Compatibility layer for running the adapter both inside Hermes and in
isolated unit tests where the gateway is not on ``sys.path``.

When ``gateway.platforms.base`` is importable we re-export the real
``BasePlatformAdapter`` / ``MessageEvent`` / ``MessageType`` / ``SendResult``
and ``gateway.session`` / ``gateway.config`` types. Otherwise we install
shape-faithful stubs so the offline test suite can construct events,
register handlers, and drive ``_dispatch_*`` directly.

The stub ``BasePlatformAdapter.handle_message`` short-circuits straight to
the registered handler. In the live gateway, ``handle_message`` does much
more (session-key building, interrupt handling, etc.), but the adapter
itself only calls it as a single entry point — both shapes are equivalent
from the adapter's perspective.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

USING_REAL_GATEWAY: bool

try:  # pragma: no cover - exercised under a live Hermes install
    from gateway.platforms.base import (  # type: ignore
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
    from gateway.session import SessionSource  # type: ignore
    from gateway.config import Platform  # type: ignore

    USING_REAL_GATEWAY = True
except Exception:  # pragma: no cover
    USING_REAL_GATEWAY = False

    class _PlatformMeta(type):
        _cache: dict[str, "Platform"] = {}

        def __call__(cls, value: str) -> "Platform":  # type: ignore[override]
            key = (value or "").strip().lower()
            if not key:
                raise ValueError("Platform value must be non-empty")
            cached = cls._cache.get(key)
            if cached is not None:
                return cached
            obj = super().__call__(key)
            cls._cache[key] = obj
            return obj

    class Platform(metaclass=_PlatformMeta):  # type: ignore[no-redef]
        """Minimal stub mirroring ``gateway.config.Platform``."""

        __slots__ = ("value",)

        def __init__(self, value: str) -> None:
            self.value = value

        def __repr__(self) -> str:
            return f"Platform({self.value!r})"

        def __eq__(self, other: object) -> bool:
            if isinstance(other, Platform):
                return self.value == other.value
            return NotImplemented

        def __hash__(self) -> int:
            return hash(("Platform", self.value))

    class MessageType(Enum):  # type: ignore[no-redef]
        TEXT = "text"
        LOCATION = "location"
        PHOTO = "photo"
        VIDEO = "video"
        AUDIO = "audio"
        VOICE = "voice"
        DOCUMENT = "document"
        STICKER = "sticker"
        COMMAND = "command"

    @dataclass
    class SessionSource:  # type: ignore[no-redef]
        platform: Platform
        chat_id: str
        chat_name: Optional[str] = None
        chat_type: str = "dm"
        user_id: Optional[str] = None
        user_name: Optional[str] = None
        thread_id: Optional[str] = None
        chat_topic: Optional[str] = None
        user_id_alt: Optional[str] = None
        chat_id_alt: Optional[str] = None
        is_bot: bool = False
        guild_id: Optional[str] = None
        parent_chat_id: Optional[str] = None
        message_id: Optional[str] = None

    @dataclass
    class MessageEvent:  # type: ignore[no-redef]
        text: str
        message_type: MessageType = MessageType.TEXT
        source: Optional[SessionSource] = None
        raw_message: Any = None
        message_id: Optional[str] = None
        platform_update_id: Optional[int] = None
        media_urls: list = field(default_factory=list)
        media_types: list = field(default_factory=list)
        reply_to_message_id: Optional[str] = None
        reply_to_text: Optional[str] = None
        auto_skill: Any = None
        channel_prompt: Optional[str] = None
        channel_context: Optional[str] = None
        internal: bool = False
        timestamp: datetime = field(default_factory=datetime.now)

    @dataclass
    class SendResult:  # type: ignore[no-redef]
        success: bool
        message_id: Optional[str] = None
        error: Optional[str] = None
        raw_response: Any = None
        retryable: bool = False
        continuation_message_ids: tuple = ()

    class _PlatformConfigStub:
        extra: dict = {}

    class BasePlatformAdapter:  # type: ignore[no-redef]
        """Test-only stub of the real ``BasePlatformAdapter``.

        Records the registered handler and exposes ``build_source`` +
        ``handle_message`` so the adapter's dispatch path can be exercised
        without a live gateway.
        """

        def __init__(self, config: Any = None, platform: Platform | None = None) -> None:
            self.config = config or _PlatformConfigStub()
            self.platform = platform or Platform("xmpp")
            self._message_handler: Callable[[MessageEvent], Awaitable[None]] | None = None

        def set_message_handler(
            self, handler: Callable[[MessageEvent], Awaitable[None]]
        ) -> None:
            self._message_handler = handler

        def build_source(
            self,
            chat_id: str,
            chat_name: Optional[str] = None,
            chat_type: str = "dm",
            user_id: Optional[str] = None,
            user_name: Optional[str] = None,
            thread_id: Optional[str] = None,
            chat_topic: Optional[str] = None,
            user_id_alt: Optional[str] = None,
            chat_id_alt: Optional[str] = None,
            is_bot: bool = False,
            guild_id: Optional[str] = None,
            parent_chat_id: Optional[str] = None,
            message_id: Optional[str] = None,
        ) -> SessionSource:
            return SessionSource(
                platform=self.platform,
                chat_id=str(chat_id),
                chat_name=chat_name,
                chat_type=chat_type,
                user_id=str(user_id) if user_id else None,
                user_name=user_name,
                thread_id=str(thread_id) if thread_id else None,
                chat_topic=chat_topic.strip() if chat_topic else None,
                user_id_alt=user_id_alt,
                chat_id_alt=chat_id_alt,
                is_bot=is_bot,
                guild_id=str(guild_id) if guild_id else None,
                parent_chat_id=str(parent_chat_id) if parent_chat_id else None,
                message_id=str(message_id) if message_id else None,
            )

        async def handle_message(self, event: MessageEvent) -> None:
            if self._message_handler is None:
                return
            await self._message_handler(event)


__all__ = [
    "BasePlatformAdapter",
    "MessageEvent",
    "MessageType",
    "Platform",
    "SendResult",
    "SessionSource",
    "USING_REAL_GATEWAY",
]
