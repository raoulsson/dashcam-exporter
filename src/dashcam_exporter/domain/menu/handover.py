"""One item's long help, composed out of two voices that stay told apart.

Items 5 and 7 are the only two entries whose work is really done by something
else, and their help screens say so out loud: the exporter's own sentences
first, then whatever the installed plugin says about its own half, quoted, set
in, and attributed under the plugin's name. Blend the two and this repo becomes
answerable for prose describing a destination it has never heard of — which is
the sentence an operator would then bring back here to complain about.

Nothing here imports pipeline, and nothing here may. items.py is imported by
tests that never build a pipeline, and that isolation is what lets the whole
menu be driven with a mock; so the paint for the plugin's name is handed in
rather than fetched.
"""

from __future__ import annotations


class Handover:
    """The hand-over from our words to the plugin's, for one item.

    Holds the two things the composition needs — the collaborator whose words
    are being quoted, and the paint that sets its name apart from ours — and
    answers one question: what does `h <n>` print under this item.

    Built in the item's constructor, beside the collaborator it quotes, so the
    pairing is settled once and cannot drift; picking the collaborator up at
    the moment of printing is how an item ends up quoting one of them under
    the other's name.

    `paint` is anything answering `yellow(text)` — pipeline.Work is the real
    one. The BOUND METHOD would be the narrower thing to hold and is not held,
    on purpose: reading it in the constructor demands it of every `work` double
    that only ever builds the menu to read its edges, and two of those exist
    that never print a help screen at all.
    """

    def __init__(self, paint, act):
        self._paint = paint
        self._act = act

    def about(self, opening) -> str:
        """The exporter's sentences, then the plugin's own, set in and attributed.

        Two things are always the exporter's to say: what the step is for, and
        where the local edition puts the result. Everything past that belongs to
        whoever is actually doing the work, so it is quoted rather than
        paraphrased, under the name of the thing that said it -- a reader can then
        tell which sentences this repo is answerable for and which it is not.
        """
        said = self._long_description()
        if not said:
            return opening
        name = self._plugin_name() or "the plugin"
        return "%s\n\nFrom the configured plugin %s:\n\n%s" % (
            opening, self._paint.yellow(name), self._tabbed(said))

    def _long_description(self):
        """An act's own long-form help, or "" if it has none. Never raises: this
        feeds a HELP SCREEN, the one place in the tool nothing should fail.

        Called by name rather than through getattr. The collaborator is one of the
        exporter's own four and its base declares this, so a missing method is now
        a construction error rather than something to shrug past here — and the
        shrug was itself the bug, since getattr with a default is exactly what
        turns "nobody implemented it" into a crash at the far end. The try stays
        for what genuinely can fail: a plugin's own answer, reached through the
        collaborator, is somebody else's code.
        """
        try:
            said = self._act.get_website_upload_description()
        except Exception:
            return ""
        return said.strip() if isinstance(said, str) else ""

    def _plugin_name(self):
        try:
            return self._act.plugin_name()
        except Exception:
            return None

    @staticmethod
    def _tabbed(text):
        """One tab onto every non-empty line, which is what marks a nested block.

        Blank lines stay blank on purpose: they are what separates paragraphs, and
        a tab on them would fuse the whole quotation into one.
        """
        return "\n".join(("\t" + ln) if ln else ln for ln in text.split("\n"))
