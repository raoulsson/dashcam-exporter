"""Small value objects shared by the menu, guards, and adapters.

Keeping these answers independent from the menu graph makes them usable by
guards and plugin implementations without importing the graph machinery.
``menu`` re-exports the names for backwards compatibility.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Tuple


class Strategy(Enum):
    UPLOADER = "uploader"
    LOCAL_PAGE = "local page"

    @classmethod
    def of(cls, plugin):
        return cls.UPLOADER if plugin is not None else cls.LOCAL_PAGE

    @classmethod
    def both(cls, value):
        return {strategy: value for strategy in cls}


class Ruling(Enum):
    GO = "go"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Verdict:
    ruling: Ruling
    reason: str = ""
    evidence: Tuple[str, ...] = ()

    @property
    def blocked(self):
        return self.ruling is Ruling.BLOCKED

    @property
    def selectable(self):
        return not self.blocked

    @classmethod
    def go(cls):
        return cls(Ruling.GO)

    @classmethod
    def satisfied(cls, reason):
        return cls(Ruling.SATISFIED, reason)

    @classmethod
    def refuse(cls, reason, evidence=()):
        return cls(Ruling.BLOCKED, reason, tuple(evidence))

    @staticmethod
    def first_reason(*reasons):
        return next(filter(None, reasons), None)

    @classmethod
    def first_block(cls, *reasons):
        stop = cls.first_reason(*reasons)
        return cls.refuse(stop) if stop else cls.go()


@dataclass(frozen=True)
class Outcome:
    completed: bool
    note: str
    performed: bool = True

    @classmethod
    def did(cls, note):
        return cls(True, note)

    @classmethod
    def stopped(cls, note, performed=False):
        return cls(False, note, performed)

    @classmethod
    def not_doing(cls, verdict):
        if verdict.blocked:
            return cls.stopped(verdict.reason)
        return cls(True, verdict.reason, performed=False)


class NotRun(Exception):
    """completed() was read before execute() ran."""


class Evidence(Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"
    NA = "not applicable"

    @property
    def applicable(self):
        return self is not Evidence.NA


class Scope(Enum):
    LOCAL = "local"
    FULL = "full"
