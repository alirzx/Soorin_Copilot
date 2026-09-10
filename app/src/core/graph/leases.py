"""Short-transaction Neo4j leases with bounded renewal heartbeats."""

from __future__ import annotations

import logging
import threading
from types import TracebackType
from typing import TypeVar

from src.core.graph.neo4j import Neo4jGraphRepository


logger = logging.getLogger(__name__)
_T = TypeVar("_T", bound="RenewingNeo4jLease")


class RenewingNeo4jLease:
    """Own one expiring lease while work executes outside Neo4j transactions."""

    def __init__(
        self,
        repository: Neo4jGraphRepository,
        *,
        lease_name: str,
        owner_id: str,
        ttl_seconds: int,
        retry_interval_seconds: float = 0.1,
    ) -> None:
        self.repository = repository
        self.lease_name = lease_name
        self.owner_id = owner_id
        self.ttl_seconds = max(3, int(ttl_seconds))
        self.retry_interval_seconds = max(0.01, float(retry_interval_seconds))
        self._stop_event = threading.Event()
        self._heartbeat: threading.Thread | None = None
        self.acquired = False
        self.lost = False

    def acquire(
        self,
        *,
        wait: bool,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return False
            if self.repository.try_acquire_lease(
                self.lease_name,
                self.owner_id,
                ttl_seconds=self.ttl_seconds,
            ):
                self.acquired = True
                self._start_heartbeat()
                return True
            if not wait:
                return False
            if cancel_event is not None:
                if cancel_event.wait(self.retry_interval_seconds):
                    return False
            else:
                self._stop_event.wait(self.retry_interval_seconds)

    def release(self) -> bool:
        self._stop_event.set()
        if self._heartbeat is not None and self._heartbeat.is_alive():
            self._heartbeat.join(timeout=max(1.0, self.ttl_seconds / 3))
        released = False
        if self.acquired:
            try:
                released = self.repository.release_lease(
                    self.lease_name,
                    self.owner_id,
                )
            except Exception:
                logger.exception(
                    "event=neo4j_lease_release_failed lease_name=%s",
                    self.lease_name,
                )
            self.acquired = False
        return released

    def __enter__(self: _T) -> _T:
        if not self.acquire(wait=True):
            raise RuntimeError(f"Unable to acquire lease {self.lease_name}.")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.release()

    def _start_heartbeat(self) -> None:
        self._stop_event.clear()
        self._heartbeat = threading.Thread(
            target=self._renew_loop,
            name=f"soorin-lease-{self.lease_name}",
            daemon=True,
        )
        self._heartbeat.start()

    def _renew_loop(self) -> None:
        interval = max(1.0, self.ttl_seconds / 3)
        while not self._stop_event.wait(interval):
            try:
                renewed = self.repository.renew_lease(
                    self.lease_name,
                    self.owner_id,
                    ttl_seconds=self.ttl_seconds,
                )
            except Exception:
                logger.exception(
                    "event=neo4j_lease_renew_failed lease_name=%s",
                    self.lease_name,
                )
                self.lost = True
                return
            if not renewed:
                self.lost = True
                logger.warning(
                    "event=neo4j_lease_lost lease_name=%s",
                    self.lease_name,
                )
                return
