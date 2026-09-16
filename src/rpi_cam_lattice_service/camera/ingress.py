"""Lattice video ingress lifecycle for the camera.

``VideoIngress`` owns exactly one SRT ingress at a time:

* ``create()`` registers a new ingress with Lattice's VideoManager under a
  fresh client-side id, and writes the SRT push URL to the ``EnvironmentFile``
  the MediaMTX unit loads (``SRT_TARGET=<url>``).
* ``delete()`` archives that ingress (``DeleteIngressStream`` archives rather
  than deletes: the record stays retrievable but stops accepting frames) and
  forgets its id.

A new id per ``create()`` matters: archived records persist under their id, so
reusing one across a Stop/Start cycle is not safe. The current id (or ``None``
when no ingress exists) is what the entity advertises as its ``Media`` item, so
the stale reference disappears from the entity on the next publish after a
``delete()``.
"""

from __future__ import annotations

import os
import threading
import uuid

from ..lattice import LatticeClient, SrtIngressInfo
from ..logging_setup import get_logger

logger = get_logger(__name__)


class VideoIngress:
    """The camera's single SRT ingress in Lattice, created and archived on demand."""

    def __init__(
        self,
        client: LatticeClient,
        *,
        entity_id: str,
        title: str,
        passphrase: str = "",
        srt_target_file: str = "",
    ) -> None:
        self._client = client
        self._entity_id = entity_id
        self._title = title
        self._passphrase = passphrase
        self._srt_target_file = srt_target_file
        self._info: SrtIngressInfo | None = None
        self._lock = threading.Lock()

    @property
    def video_id(self) -> str | None:
        """The live ingress id to advertise on the entity, or ``None``."""
        with self._lock:
            return self._info.video_id if self._info else None

    @property
    def info(self) -> SrtIngressInfo | None:
        with self._lock:
            return self._info

    def create(self) -> SrtIngressInfo:
        """Register a new ingress and persist its push URL for MediaMTX.

        Raises on failure and leaves the previous state untouched. If an
        ingress already exists it is returned as-is (create is idempotent
        within a Start).
        """
        with self._lock:
            if self._info is not None:
                return self._info
            ingress_id = f"{self._entity_id}-{uuid.uuid4()}"
            info = self._client.create_srt_ingress(
                title=self._title,
                passphrase=self._passphrase,
                ingress_id=ingress_id,
            )
            logger.info(
                "registered SRT ingress", video_id=info.video_id, push_url=info.push_url
            )
            self._write_srt_target(info.push_url)
            self._info = info
            return info

    def delete(self) -> None:
        """Archive the current ingress in Lattice and forget it.

        Raises on failure and keeps the id so a retry can archive it later.
        """
        with self._lock:
            if self._info is None:
                return
            video_id = self._info.video_id
            self._client.delete_srt_ingress(video_id)
            logger.info("archived SRT ingress", video_id=video_id)
            self._info = None

    def _write_srt_target(self, push_url: str) -> None:
        """Write the SRT push URL as an EnvironmentFile for the MediaMTX unit."""
        path = self._srt_target_file
        if not path:
            return
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w") as handle:
            handle.write(f"SRT_TARGET={push_url}\n")
        logger.info("wrote SRT target for MediaMTX", srt_target_file=path)
