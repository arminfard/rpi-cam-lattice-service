"""Lattice API plumbing: TLS transport, auth metadata, and one thin client per
Lattice API (entities, video, tasks) behind the ``LatticeClient`` facade."""

from .client import LatticeClient
from .video import SrtIngressInfo

__all__ = ["LatticeClient", "SrtIngressInfo"]
