#!/usr/bin/env python3
"""The hand-over on the help screen: two voices, told apart.

Items 5 and 7 print the exporter's own sentences and then, when something else
is really doing the work, that thing's own words quoted under its name. What
these pin is the SEAM rather than the prose: whose sentence is whose, that a
collaborator with nothing to add leaves no trace at all, and that a plugin
raising in the middle of a help screen cannot take the screen down.

Nothing here imports pipeline, deliberately — handover.py holds a painter and
a collaborator and nothing else, and a test that had to build a pipeline to
reach it would be evidence the module had grown a dependency it must not have.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dashcam_exporter.handover import Handover       # noqa: E402


OPENING = "This step builds the website.\n\nIn local mode it writes a page."


class Paint:
    """Stands in for pipeline.Work, which lends the items its colours.

    Marks rather than colours, so a test can say WHICH text was painted
    without spelling an escape sequence in every assertion.
    """

    def __init__(self):
        self.painted = []

    def yellow(self, text):
        self.painted.append(text)
        return "<%s>" % text


class Act:
    """A publishing collaborator, as much of one as this seam touches."""

    def __init__(self, said="", name=None):
        self._said = said
        self._name = name

    def get_website_upload_description(self):
        return self._said

    def plugin_name(self):
        return self._name


class Raises(Act):
    """Somebody else's code, behaving like somebody else's code."""

    def get_website_upload_description(self):
        raise RuntimeError("the plugin is unhappy")

    def plugin_name(self):
        raise RuntimeError("and will not say who it is")


def about(act, paint=None):
    return Handover(paint or Paint(), act).about(OPENING)


class TestACollaboratorWithNothingToAdd(unittest.TestCase):
    """The case that made the class worth having.

    The local edition's halves answer "" on purpose — the exporter has already
    said everything true about them — and the screen has to end exactly where
    it ended before. A dangling "From the configured plugin:" under an empty
    quotation is worse than saying nothing: it tells the operator a plugin is
    configured and then shows him nothing it said.
    """

    def test_the_opening_comes_back_untouched(self):
        self.assertEqual(about(Act(said="")), OPENING)

    def test_there_is_no_attribution_block_and_no_dangling_colon(self):
        said = about(Act(said=""))
        self.assertNotIn("From the configured plugin", said)
        self.assertNotIn(":\n\n\t", said)
        self.assertFalse(said.rstrip().endswith(":"))

    def test_nothing_is_painted_when_nothing_is_quoted(self):
        paint = Paint()
        about(Act(said=""), paint)
        self.assertEqual(paint.painted, [],
                         "a name was painted for a quotation that is not there")

    def test_whitespace_is_not_an_answer(self):
        """A plugin returning a blank line has added nothing, and the screen
        must not grow a heading over it."""
        self.assertEqual(about(Act(said="   \n\n  ")), OPENING)

    def test_something_that_is_not_a_string_is_not_an_answer(self):
        self.assertEqual(about(Act(said=None)), OPENING)


class TestTheHandoverItself(unittest.TestCase):
    def test_the_exporters_own_words_come_first_and_whole(self):
        said = about(Act(said="We push to a bucket.", name="Bucket"))
        self.assertTrue(said.startswith(OPENING))

    def test_the_plugins_words_are_quoted_under_its_name(self):
        said = about(Act(said="We push to a bucket.", name="Bucket"))
        self.assertIn("From the configured plugin <Bucket>:", said)
        self.assertIn("\tWe push to a bucket.", said)

    def test_the_name_is_the_painted_thing(self):
        paint = Paint()
        about(Act(said="Words.", name="Bucket"), paint)
        self.assertEqual(paint.painted, ["Bucket"])

    def test_a_plugin_that_will_not_name_itself_is_still_not_us(self):
        """Attribution to "the plugin" rather than none. The words are still
        not the exporter's, and an unattributed quotation reads as ours."""
        said = about(Act(said="Words.", name=None))
        self.assertIn("From the configured plugin <the plugin>:", said)


class TestTheQuotationIsSetIn(unittest.TestCase):
    def test_every_non_empty_line_is_tabbed(self):
        said = about(Act(said="One.\nTwo.", name="P"))
        self.assertIn("\tOne.\n\tTwo.", said)

    def test_a_blank_line_stays_blank(self):
        """Paragraphs inside the quotation survive. A tab on the blank line
        fuses the whole quotation into one block."""
        said = about(Act(said="One.\n\nTwo.", name="P"))
        self.assertIn("\tOne.\n\n\tTwo.", said)


class TestAHelpScreenNeverFails(unittest.TestCase):
    """The one screen in the tool that exists to tell an operator who to
    complain to. A plugin raising inside it must not be what takes it down."""

    def test_a_plugin_that_raises_leaves_the_opening_standing(self):
        self.assertEqual(about(Raises()), OPENING)

    def test_a_name_that_raises_still_attributes_the_quotation(self):
        class Nameless(Act):
            def plugin_name(self):
                raise RuntimeError("no")

        said = about(Nameless(said="Words."))
        self.assertIn("From the configured plugin <the plugin>:", said)


class TestItIsNotABagOfStatics(unittest.TestCase):
    """What the class is FOR, asserted rather than left to the eye.

    It holds its two collaborators and answers one question, so an item builds
    it once beside the collaborator it quotes. Written as free functions this
    was four names in items.py that each had to be handed the same two
    arguments at every call site, and the caller could pair the builder's words
    with the publisher's name without anything noticing.
    """

    def test_the_collaborator_is_held_not_passed(self):
        import inspect
        self.assertEqual(list(inspect.signature(Handover.about).parameters),
                         ["self", "opening"])

    def test_two_handovers_over_one_painter_stay_apart(self):
        paint = Paint()
        build = Handover(paint, Act(said="We build.", name="Builder"))
        send = Handover(paint, Act(said="We send.", name="Sender"))
        self.assertIn("\tWe build.", build.about(OPENING))
        self.assertIn("<Builder>", build.about(OPENING))
        self.assertIn("\tWe send.", send.about(OPENING))
        self.assertIn("<Sender>", send.about(OPENING))


if __name__ == "__main__":
    unittest.main(verbosity=2)
