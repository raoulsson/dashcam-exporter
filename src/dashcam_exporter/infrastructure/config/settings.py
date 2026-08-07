from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    """Raw, immutable application settings with explicit typed accessors."""

    _values: dict[str, str]

    def text(self, key: str, default: str) -> str:
        return self._values.get(key, default)

    def integer(self, key: str, default: int) -> int:
        value = self._values.get(key, "").strip()
        return int(value) if value else default

    def decimal(self, key: str, default: float) -> float:
        value = self._values.get(key, "").strip()
        return float(value) if value else default

    def flag(self, key: str, default: bool) -> bool:
        value = self._values.get(key)
        return default if value is None else value.strip().lower() in {"true", "yes", "1", "on"}

    def path(self, key: str, default: Path) -> Path:
        return Path(self.text(key, str(default))).expanduser()
