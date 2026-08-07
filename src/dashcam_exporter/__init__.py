"""Dashcam exporter package and its public namespace entry points."""

from .infrastructure.adapters import DdpaiDataAdapter, ExporterAdapter

# Package-level module names are deliberate composition-root conveniences for
# plugins and existing semantic fixtures; implementation lives in namespaces.
__all__ = [
    "DdpaiDataAdapter", "ExporterAdapter", "guards", "items", "menu",
    "pipeline", "renderer", "uploader", "world",
]


def __getattr__(name):
    """Resolve legacy package-level module names without eager imports."""
    from importlib import import_module

    targets = {
        "guards": ".domain.menu.guards",
        "items": ".domain.menu.items",
        "menu": ".domain.menu.menu",
        "pipeline": ".application.workflow.pipeline",
        "renderer": ".infrastructure.media.renderer",
        "uploader": ".application.ports.uploader",
        "world": ".domain.model.world",
    }
    target = targets.get(name)
    if target is None:
        raise AttributeError(name)
    module = import_module(target, __name__)
    globals()[name] = module
    return module
