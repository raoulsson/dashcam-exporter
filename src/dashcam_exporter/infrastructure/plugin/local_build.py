"""Item 5 for the built-in export plugin: this repo's own local build, adapted.

An install that only wants its finished folder copied to a disk must still get
the LOCAL EDITION'S deliverable out of item 5 -- the self-contained page and
the gather into final_<day>_<import> -- because that folder is the thing item 8
then copies. Configuring a destination is not a request for a different build.

So nothing here builds anything. It holds the exporter's own LocalPage and
passes the call through, which is why the local page can never drift from what
an install with no destination produces: there is one implementation of it and
this is not it.

WHAT THE ADAPTER IS FOR is the argument. A plugin's act is handed a Workspace
-- the reduced, plugin-facing view -- and LocalPage is handed the exporter's
World. The two agree about everything LocalPage actually reads except
`final_folders`, which the Workspace does not carry (nothing outside this repo
has any business knowing that the deliverable was gathered). That one field is
supplied here, from the same callable the exporter enumerates them with, and
the rest is the Workspace's own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from dashcam_exporter.application.ports.uploader import Builder


@dataclass(frozen=True)
class _Gathered:
    """The two fields LocalPage reads off a World, and nothing else.

    Deliberately not a World: a real one carries card facts, ledger marks and
    the destination's own answers, and constructing one here would be this
    module claiming to know what a hundred other fields should say. LocalPage's
    own guard asks for renders or a gathered folder; that is what it is given.
    """

    renders: Tuple = ()
    final_folders: Tuple = ()


class LocalBuild(Builder):
    """Item 5: the exporter's local page build, wearing the plugin interface."""

    def __init__(self, local, finals):
        self._local = local
        self._finals = finals

    def describe(self) -> str:
        """Not LocalPage's own sentence, and that is the one deviation.

        It says "Nothing leaves this machine", which was true of the edition it
        was written for and is not true here: item 8 copies this very folder to
        a destination. A row that reads as a promise has to keep it.
        """
        return ("Build the local result page and gather the renders into one "
                "folder for 8) to copy out.")

    def evaluate(self, workspace):
        return self._local.evaluate(self._world_of(workspace))

    def execute(self, workspace):
        return self._local.execute(self._world_of(workspace))

    def _world_of(self, workspace):
        return _Gathered(renders=tuple(getattr(workspace, "renders", ())),
                         final_folders=tuple(self._finals()))
