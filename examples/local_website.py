"""A complete, working plugin: build a small site in /tmp, then "ftp" it out.

The point of this file is to be READ. It is the shortest thing that is still a
real implementation of both acts, so what is left is the shape every publisher
has to have and nothing else. Point `website_uploader` at it in config.txt (or
SET_WEBSITE_UPLOADER in .env, which is the same setting and stays out of git):

    website_uploader = ~/dev/dashcam-exporter/examples/local_website.py:LocalWebSiteBuilderPlugin:LocalWebSiteUploader

One plugin, two classes: the builder is item 6, the uploader is item 7. Where
things go is this file's own business, not the exporter's — the two directories
below come from the environment, and the exporter has exactly one setting about
publishing, which is which plugin to load.

THE ONE RULE THIS EXAMPLE EXISTS TO TEACH

The builder COPIES what it needs out of the workspace into a staging directory
of its own and builds there. It never moves, renames or deletes anything under
the workspace, and never writes into it. That is not tidiness: the renders and
sidecars it is handed are the same files the exporter's erase gates then reason
about, so a mv would leave items 4, 8 and 9 judging a workspace that shifted
under them. Read the workspace; write only where you own the ground.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from uploader import (Builder, Evidence, Outcome, Uploader, Verdict, blocked,
                      did, go, satisfied, stopped)


def _staging() -> Path:
    """Where the site is built. Under the system temp dir, never under <out>."""
    return Path(os.environ.get("DASHCAM_LOCAL_SITE_STAGING")
                or (Path(tempfile.gettempdir()) / "dashcam-site")).expanduser()


def _destination() -> Path:
    """The far end. A directory here; a real one is an FTP or S3 endpoint."""
    return Path(os.environ.get("DASHCAM_LOCAL_SITE_DEST")
                or "~/dashcam-published").expanduser()


def _page(title: str, body: str) -> str:
    return ("<!doctype html><meta charset=utf-8><title>%s</title>"
            "<link rel=stylesheet href=style.css><h1>%s</h1>%s"
            % (title, title, body))


STYLE = "body{font:16px/1.5 system-ui;margin:3rem auto;max-width:40rem}\n"


class LocalWebSiteBuilderPlugin(Builder):
    """Item 6: stage a tiny site from the renders and sidecars."""

    def describe(self) -> str:
        return "Build a small HTML site in %s from the renders." % _staging()

    def evaluate(self, workspace) -> Verdict:
        """Cheap and local: this is asked on every menu draw.

        Not "have the renders been made" — the exporter answers that itself
        before it ever gets here, and an act that re-asked would be answering a
        question about ordering. This asks about the one thing that is this
        plugin's own business: somewhere to build.
        """
        if _staging().parent.is_dir():
            return go()
        return blocked("%s does not exist, so nothing can be staged there"
                       % _staging().parent)

    def execute(self, workspace) -> Outcome:
        """COPY out, then build in our own directory.

        Every path below is under the staging dir. Nothing is written under
        workspace.out_dir and nothing there is moved — that tree belongs to the
        exporter, whose erase gates are about to reason over the very files
        being read here.
        """
        staged = self._staged(workspace)
        trips = self._carried(workspace)
        self._write_pages(staged, trips)
        workspace.ui.say("  staged %d trips in %s" % (len(trips), staged))
        return did("%d trips staged in %s" % (len(trips), staged))

    def _staged(self, workspace) -> Path:
        """A clean staging dir, and the renders copied into it.

        copy2, never move: see the module docstring. The exporter hands over
        the real paths precisely because an implementation may need the bytes,
        and it trusts that reading them is all that happens.
        """
        staged = _staging()
        shutil.rmtree(str(staged), ignore_errors=True)
        (staged / "videos").mkdir(parents=True, exist_ok=True)
        for render in workspace.renders:
            shutil.copy2(str(render.path), str(staged / "videos" / render.name))
        return staged

    def _carried(self, workspace):
        """The trips this site shows: everything but the deliberate drops.

        workspace.dropped_ids is the exporter telling us a trip was deleted ON
        PURPOSE. Without it a rebuild cannot tell that from a trip that was
        published and then cleaned up locally — and carrying THAT one forward
        is the whole reason "delete local after publish" is safe.
        """
        dropped = set(workspace.dropped_ids)
        return list(filter(lambda t: t not in dropped, workspace.trip_ids))

    def _write_pages(self, staged: Path, trips) -> None:
        (staged / "style.css").write_text(STYLE, encoding="utf-8")
        for trip in trips:
            _write_trip_page(staged, trip)
        _write_index(staged, trips)


# The transport, as PSEUDO CODE. It cannot run and is not meant to: it is here
# to show the shape of the thing a real implementation puts in execute(), which
# is an ssh, an rsync, an aws s3 sync, a curl, or a genuine FTP session.
FTP_SESSION = """\
    session = ftp.connect(host, user, password)      # PSEUDO CODE
    for page in staged.rglob("*"):                   # PSEUDO CODE
        session.store(page, remote_root / page.name) # PSEUDO CODE
    session.quit()                                   # PSEUDO CODE
"""


class LocalWebSiteUploader(Uploader):
    """Item 7: send the staged site to the destination, and answer for it.

    The destination here is another directory, so the example can be run and
    tested without a network. The copy below stands exactly where a real
    implementation's transport goes, and FTP_SESSION above sketches that.
    """

    def describe(self) -> str:
        return "Send the staged site to %s." % _destination()

    def evaluate(self, workspace) -> Verdict:
        """Is there something to send, and is anything left to do.

        SATISFIED is not GO: with the staged site already at the destination
        there is nothing owed, and the exporter moves on without calling
        execute(). Both questions are answered off local stat calls, because
        this is asked on every menu draw — the question that may go to the far
        end is is_complete(), and it is asked at most a few times per dispatch.
        """
        if not (_staging() / "index.html").is_file():
            return blocked("nothing staged in %s yet" % _staging())
        return self._anything_left()

    def _anything_left(self) -> Verdict:
        if self._missing():
            return go()
        return satisfied("%s already has the staged site" % _destination())

    def execute(self, workspace) -> Outcome:
        dest = _destination()
        workspace.ui.say("  a real uploader runs its transport here, roughly:")
        for line in FTP_SESSION.splitlines():
            workspace.ui.say("  " + line)
        sent = self._send(dest)
        return _sent(sent, dest)

    def _send(self, dest: Path):
        dest.mkdir(parents=True, exist_ok=True)
        sent = self._missing()
        for rel in sent:
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(_staging() / rel), str(dest / rel))
        return sent

    def is_complete(self, trip_ids) -> Evidence:
        """Is EVERY one of these trips at the destination.

        All or nothing, and the list is the exporter's: a trip that was never
        rendered is in it, has no page here, and makes the answer NO — which is
        exactly what keeps its footage from being erased.

        NO rather than UNKNOWN when a page is missing: a directory that can be
        listed is a destination that answered. UNKNOWN belongs to the case
        where the far end could not be reached at all, which for a real
        transport is the common one and which fails closed.
        """
        there = _names_at(_destination())
        if there is None:
            return Evidence.UNKNOWN         # could not look: not "no", never "yes"
        return _yes_no(_all_pages_at(trip_ids, there))

    def _missing(self):
        """Staged paths, relative to the staging dir, that are not at the far
        end at the same size."""
        staged = _staging()
        return list(filter(lambda rel: _differs(staged, rel), _relative_files(staged)))


def _write_trip_page(staged: Path, trip: str) -> None:
    (staged / (trip + ".html")).write_text(
        _page(trip, "<video controls src='videos/%s_h1080.mp4'></video>" % trip),
        encoding="utf-8")


def _write_index(staged: Path, trips) -> None:
    (staged / "index.html").write_text(
        _page("Drives", "".join(map(_link, trips))), encoding="utf-8")


def _link(trip: str) -> str:
    return "<li><a href='%s.html'>%s</a>" % (trip, trip)


def _names_at(base: Path):
    """What is at the far end, or None because it could not be listed."""
    try:
        return set(map(_name_of, base.iterdir()))
    except OSError:
        return None


def _name_of(path: Path) -> str:
    return path.name


def _all_pages_at(trip_ids, there) -> bool:
    return all(map(lambda t: (t + ".html") in there, trip_ids))


def _relative_files(staged: Path):
    return list(map(lambda p: p.relative_to(staged), _files_under(staged)))


def _differs(staged: Path, rel) -> bool:
    return _size(staged / rel) != _size(_destination() / rel)


def _files_under(base: Path):
    return sorted(filter(_is_file, _rglob(base)))


def _rglob(base: Path):
    if not base.is_dir():
        return []
    return base.rglob("*")


def _is_file(path: Path) -> bool:
    return path.is_file()


def _size(path: Path):
    try:
        return path.stat().st_size
    except OSError:
        return None


def _yes_no(ok: bool) -> Evidence:
    return Evidence.YES if ok else Evidence.NO


def _sent(sent, dest: Path) -> Outcome:
    if sent:
        return did("%d files sent to %s" % (len(sent), dest))
    return stopped("nothing was staged to send")
