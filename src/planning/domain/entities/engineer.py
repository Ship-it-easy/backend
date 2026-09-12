from dataclasses import dataclass

from planning.domain.entities.coordinate import Coordinate
from planning.domain.enums import TransportType


@dataclass(frozen=True)
class Engineer:
    id: int
    transport_type: TransportType
    coordinate: Coordinate
    shift_start_min: int
    shift_end_min: int
    qualifications: frozenset[int]
