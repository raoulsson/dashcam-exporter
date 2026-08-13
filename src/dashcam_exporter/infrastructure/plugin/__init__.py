"""The plugin this repo ships with itself: build locally, copy the result out.

It exists so that an install with nowhere to publish to but somewhere to PUT
the result does not need a second path through the menu. Point
`website_export_dir` at a directory and the exporter constructs the pair below
as an ordinary uploader.Plugin: item 5 builds the local page and gathers as it
always did, item 8 copies that folder to the directory, and every gate
downstream -- the step graph, item 8's availability, the frozen destination
answer, the erase in item 9 -- works because it is looking at a plugin and does
not care where the plugin came from.

`upload_plugin` WINS if both are set. Two publishers disagreeing about where
the footage went is how the only copy gets erased, so the exporter says at
startup which of the two it is using rather than deciding quietly.
"""

from dashcam_exporter.application.ports.uploader import Plugin
from dashcam_exporter.infrastructure.plugin.directory_export import DirectoryExport
from dashcam_exporter.infrastructure.plugin.local_build import LocalBuild


class ExportPlugin(Plugin):
    """The built-in pair, distinguishable from one somebody else supplied.

    A type rather than a flag on the ctx, because two things have to tell the
    two apart and both of them already hold the plugin: the configuration
    screen, which must not describe this as "a user plugin handles build and
    upload", and item 5's collaborator, which must not file a second step
    result for a build that logs itself.

    It subclasses Plugin rather than wrapping one so that everything reading
    .builder, .uploader, .name and .origin off a plugin goes on working without
    knowing this exists.
    """

    @property
    def origin(self) -> str:
        """Who answered, for the erase banner. Composed, so it cannot go stale.

        The spec of a built-in is not a path anybody typed, so the destination
        is named instead: that is the thing the operator would go and look at
        after reading that his footage was confirmed somewhere.
        """
        return "%s (built in, copying to %s)" % (self.name,
                                                 self.uploader.destination)


def export_plugin(local, finals, destination, record) -> ExportPlugin:
    """The built-in plugin, wired to one destination.

    `local` is the exporter's own item-5 collaborator and `finals` the callable
    that enumerates the gathered folders. Both are handed in rather than
    imported: this package is infrastructure and the local build is the
    application's, and inverting that import would put the workflow behind an
    adapter that is meant to sit in front of it.
    """
    return ExportPlugin(LocalBuild(local, finals),
                        DirectoryExport(destination, finals, record),
                        "built-in:LocalBuild:DirectoryExport")


__all__ = ["DirectoryExport", "ExportPlugin", "LocalBuild", "export_plugin"]
