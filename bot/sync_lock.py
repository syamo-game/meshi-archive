from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import blake2b
from threading import Lock
from _thread import LockType

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session


class SyncAlreadyRunning(RuntimeError):
    pass


_registry_lock: LockType = Lock()
_channel_locks: dict[str, LockType] = {}


def sync_lock_key(channel_id: str) -> int:
    digest = blake2b(f"meshi-history-sync:{channel_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


@contextmanager
def lock_channel_sync(db: Session, channel_id: str) -> Iterator[None]:
    with _registry_lock:
        local_lock = _channel_locks.setdefault(channel_id, Lock())
    if not local_lock.acquire(blocking=False):
        raise SyncAlreadyRunning(f"Channel sync is already running: channel_id={channel_id}")
    connection: Connection | None = None
    acquired: bool = False
    try:
        bind = db.get_bind()
        if bind.dialect.name == "postgresql":
            engine = bind.engine if isinstance(bind, Connection) else bind
            connection = engine.connect()
            acquired = bool(connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": sync_lock_key(channel_id)},
            ))
            connection.commit()
            if not acquired:
                raise SyncAlreadyRunning(f"Channel sync is already running: channel_id={channel_id}")
        elif bind.dialect.name != "sqlite":
            raise RuntimeError(f"Channel sync does not support database={bind.dialect.name}")
        yield
    finally:
        try:
            if connection is not None:
                try:
                    if acquired:
                        released = connection.scalar(
                            text("SELECT pg_advisory_unlock(:key)"), {"key": sync_lock_key(channel_id)},
                        )
                        connection.commit()
                        if not released:
                            raise RuntimeError(f"Channel sync lock was lost: channel_id={channel_id}")
                finally:
                    connection.close()
        finally:
            local_lock.release()
