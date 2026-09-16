"""The sensor-source abstraction.

``State`` is a plain-Python snapshot of what your sensors give you.
deliberately decoupled from any Lattice/protobuf type. A ``Source`` produces a
``State`` on each call. 
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class State:
    """A single observation from a sensor source.

    Positions are geodetic; velocity/acceleration are in the local ENU frame
    (East, North, Up), matching the Lattice ``Location`` model.
    """

    latitude_degrees: float
    longitude_degrees: float
    altitude_hae_meters: float
    altitude_agl_meters: float
    altitude_asf_meters: float

    velocity_e_mps: float
    velocity_n_mps: float
    velocity_u_mps: float
    speed_mps: float

    acceleration_e_mps2: float
    acceleration_n_mps2: float
    acceleration_u_mps2: float


class Source(ABC):
    """A source of sensor observations."""

    @abstractmethod
    def next_state(self) -> State:
        """Advance the source and return the current observation."""
        raise NotImplementedError
