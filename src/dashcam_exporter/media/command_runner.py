from abc import ABC, abstractmethod
from collections.abc import Sequence


class CommandRunner(ABC):
    """Port for a process executor, enabling deterministic unit tests."""

    @abstractmethod
    def run(self, command: Sequence[str]) -> None:
        """Run a command or raise an exception describing its failure."""
