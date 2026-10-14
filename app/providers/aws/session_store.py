from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

from app.providers.aws.credentials import AWSCredentials


@dataclass
class AWSSessionEntry:
    credentials: AWSCredentials
    expires_at: datetime


class AWSCredentialSessionStore:
    """
    Legacy transient credential cache.

    Entries are scoped by (user_id, aws_connection_id) so switching between
    client workspaces cannot accidentally reuse another connection's keys.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[int, int], AWSSessionEntry] = {}
        self._lock = asyncio.Lock()

    async def set(
        self,
        *,
        user_id: int,
        aws_connection_id: int,
        credentials: AWSCredentials,
    ) -> None:
        async with self._lock:
            self._entries[(user_id, aws_connection_id)] = AWSSessionEntry(
                credentials=credentials,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=60),
            )

    async def get(
        self,
        *,
        user_id: int,
        aws_connection_id: int,
    ) -> AWSCredentials | None:
        async with self._lock:
            entry = self._entries.get((user_id, aws_connection_id))
            if entry is None:
                return None
            if datetime.now(timezone.utc) >= entry.expires_at or not entry.credentials.valid_for(0):
                self._entries.pop((user_id, aws_connection_id), None)
                return None
            return entry.credentials

    async def remove(
        self,
        *,
        user_id: int,
        aws_connection_id: int | None = None,
    ) -> None:
        async with self._lock:
            if aws_connection_id is not None:
                self._entries.pop((user_id, aws_connection_id), None)
                return

            keys = [key for key in self._entries if key[0] == user_id]
            for key in keys:
                self._entries.pop(key, None)


aws_credential_sessions = AWSCredentialSessionStore()
