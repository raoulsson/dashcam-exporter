import re
from pathlib import Path

from .settings import Settings


class FileSettingsLoader:
    """Loads a simple ordered ``key=value`` configuration file."""

    _reference = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

    def load(self, path: Path) -> Settings:
        if not path.is_file():
            return Settings({})
        values: dict[str, str] = {}
        for source_line in path.read_text(encoding="utf-8").splitlines():
            line = source_line.split("#", 1)[0].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = self._expand(value.strip(), values)
        return Settings(values)

    def _expand(self, value: str, values: dict[str, str]) -> str:
        return self._reference.sub(lambda match: values.get(match.group(1), match.group(0)), value)
