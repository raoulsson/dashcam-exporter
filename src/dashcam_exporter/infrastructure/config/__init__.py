from .file_settings_loader import FileSettingsLoader
from .settings import Settings

import re

PRIVATE_KEYS = ("upload_plugin", "home_lat", "home_lon", "card")

def load_config(path):
    out = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                                  lambda m: out.get(m.group(1), m.group(0)),
                                  value.strip())
    return out

def load_env(path):
    out = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out

def as_bool(value, default=False):
    text = (value or "").strip().lower()
    return default if not text else text in ("1", "true", "yes", "on")

def card_root(configured):
    level = [configured]
    for _ in range(4):
        found = next((p for p in level if (p / "DCIM").is_dir()), None)
        if found:
            return found
        children = []
        for p in level:
            try:
                children.extend(sorted(c for c in p.iterdir()
                                       if not c.is_symlink() and c.is_dir()))
            except OSError:
                pass
        level = children
        if not level:
            break
    return configured

__all__ = ["FileSettingsLoader", "Settings", "PRIVATE_KEYS", "load_config",
           "load_env", "as_bool", "card_root"]
