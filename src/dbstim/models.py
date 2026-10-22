"""Immutable assignment values; presentation belongs to the task framework."""

from dataclasses import dataclass
import math
import re
from typing import Literal, TypeAlias

JSONValue: TypeAlias = str | int | float | bool | None | list['JSONValue'] | dict[str, 'JSONValue']
JSONObject: TypeAlias = dict[str, JSONValue]
TrialStatus: TypeAlias = Literal['started', 'completed']
SessionStatus: TypeAlias = Literal['completed', 'interrupted', 'failed']
MediaMode: TypeAlias = Literal['local', 'cache', 'remote']
Operator: TypeAlias = Literal['gt', 'lt', 'eq', 'gte', 'lte', 'is', 'is_not', '>', '<', '==', '>=', '<=']
_SYMBOLS = {'>': 'gt', '<': 'lt', '==': 'eq', '>=': 'gte', '<=': 'lte'}


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', value):
        raise ValueError('IDs must be 1–128 ASCII letters, digits, underscores or hyphens')
    return value


@dataclass(frozen=True)
class MetricFilter:
    metric_id: str
    operator: Operator
    value: int | float | bool | None

    def __post_init__(self):
        if not isinstance(self.metric_id, str) or not self.metric_id or not self.metric_id.isprintable():
            raise ValueError('metric_id must be a printable string')
        if isinstance(self.operator, str) and self.operator in _SYMBOLS:
            object.__setattr__(self, 'operator', _SYMBOLS[self.operator])
        if self.operator not in ('gt', 'lt', 'eq', 'gte', 'lte', 'is', 'is_not'):
            raise ValueError('Unsupported metric operator')
        if self.value is not None and (type(self.value) not in (int, float, bool) or not math.isfinite(self.value)):
            raise ValueError('Metric values must be finite numbers, booleans or null')

    def to_dict(self) -> JSONObject:
        return dict(metric_id=self.metric_id, operator=self.operator, value=self.value)


@dataclass(frozen=True)
class Metric:
    id: str
    label: str
    kind: str
    unit: str | None
    operators: tuple[str, ...]

    def __repr__(self) -> str:
        unit = f', {self.unit}' if self.unit else ''
        return f"<Metric {self.id}: {self.label} ({self.kind}{unit}); operators {' '.join(self.operators)}>"


@dataclass(frozen=True)
class Segment:
    start_seconds: float
    end_seconds: float

    def __post_init__(self):
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for value in (self.start_seconds, self.end_seconds)) or not 0 <= self.start_seconds < self.end_seconds:
            raise ValueError('Segment must have finite bounds with 0 <= start < end')


@dataclass(frozen=True)
class Trial:
    trial_id: str
    stimulus_id: str
    position: int
    role: Literal['parent', 'repeat', 'foil']
    media_type: Literal['video']
    segment: Segment | None

    def __post_init__(self):
        identifier(self.trial_id)
        if not isinstance(self.stimulus_id, str) or not self.stimulus_id or not self.stimulus_id.isprintable():
            raise ValueError('Invalid stimulus identity')
        if type(self.position) is not int or self.position < 1:
            raise ValueError('Trial position must be a positive integer')
        if self.role not in ('parent', 'repeat', 'foil') or self.media_type != 'video':
            raise ValueError('Unsupported role or media type')
        if self.role == 'foil' and self.segment is None:
            raise ValueError('A foil requires an assigned segment')
