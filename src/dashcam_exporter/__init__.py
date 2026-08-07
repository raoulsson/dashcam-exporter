"""Dashcam exporter package and its public namespace entry points."""

from .infrastructure.adapters import DdpaiDataAdapter, ExporterAdapter

# Package-level module names are deliberate composition-root conveniences for
# plugins and existing semantic fixtures; implementation lives in namespaces.
from .application.ports import uploader
from .application.workflow import pipeline
from .domain.menu import guards, items, menu
from .domain.model import world
from .infrastructure.media import renderer

__all__ = [
    "DdpaiDataAdapter", "ExporterAdapter", "guards", "items", "menu",
    "pipeline", "renderer", "uploader", "world",
]
