"""The questions the pipeline asks about a tree, answered by whoever wrote it."""

import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.application.workflow import card_access


def ddpai_card(root: Path) -> Path:
    (root / "DCIM/200video/front").mkdir(parents=True)
    (root / "DCIM/200video/rear").mkdir(parents=True)
    (root / "DCIM/203gps/tar").mkdir(parents=True)
    return root


def blackvue_card(root: Path) -> Path:
    (root / "BlackVue/Record").mkdir(parents=True)
    return root


class CardAccessTest(unittest.TestCase):
    def setUp(self):
        card_access.forget()

    def test_recognises_a_card_from_any_supported_camera(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ddpai_card(root / "a")
            blackvue_card(root / "b")

            self.assertTrue(card_access.is_card(root / "a"))
            self.assertTrue(card_access.is_card(root / "b"))

    def test_a_tree_no_adapter_claims_is_not_a_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "DCIM/100CANON").mkdir(parents=True)

            self.assertFalse(card_access.is_card(root))
            self.assertIsNone(card_access.clip_count(root))
            self.assertEqual(card_access.stamps_on(root), set())

    def test_counts_clips_by_parsing_rather_than_by_listing(self):
        # A directory listing counted two files this tool cannot read as
        # clips on a real card. Parsing is the honest count.
        with tempfile.TemporaryDirectory() as temporary:
            card = ddpai_card(Path(temporary))
            (card / "DCIM/200video/front/20260806170529_0060.mp4").touch()
            (card / "DCIM/200video/front/20260806170629_0060.mp4").touch()
            (card / "DCIM/200video/front/notes.txt").touch()

            self.assertEqual(card_access.clip_count(card), 2)

    def test_stamps_come_back_in_our_canonical_form(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = blackvue_card(Path(temporary))
            (card / "BlackVue/Record/20210127_155052_NF.mp4").touch()

            self.assertEqual(card_access.stamps_on(card), {"20210127155052"})

    def test_front_videos_are_the_clips_main_camera_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = ddpai_card(Path(temporary))
            for stamp in ("20260806170629", "20260806170529"):
                (card / ("DCIM/200video/front/%s_0060.mp4" % stamp)).touch()

            names = [p.name for p in card_access.front_videos(card)]

        self.assertEqual(names, ["20260806170529_0060.mp4",
                                 "20260806170629_0060.mp4"])

    def test_a_track_artifact_is_whatever_that_camera_calls_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = ddpai_card(Path(temporary))

            self.assertTrue(card_access.is_track_artifact(
                card, Path("20260806170529_0540.git")))
            self.assertFalse(card_access.is_track_artifact(
                card, Path("20260806170529_0060.mp4")))

    def test_carries_track_sees_the_archive_a_ddpai_card_keeps(self):
        with tempfile.TemporaryDirectory() as temporary:
            card = ddpai_card(Path(temporary))

            self.assertFalse(card_access.carries_track(card))
            (card / "DCIM/203gps/tar/20260806170529_0540.git").touch()

            card_access.forget()
            self.assertTrue(card_access.carries_track(card))


    def test_card_root_of_walks_up_from_a_directory_inside_the_card(self):
        # Any fixed parent count is one camera's count: DCIM/200video/front
        # is three levels down and BlackVue/Record is two.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ddpai_card(root / "d")
            blackvue_card(root / "b")

            self.assertEqual(
                card_access.card_root_of(root / "d/DCIM/200video/front"),
                root / "d")
            self.assertEqual(
                card_access.card_root_of(root / "b/BlackVue/Record"),
                root / "b")

    def test_card_root_of_answers_none_when_no_ancestor_is_a_card(self):
        with tempfile.TemporaryDirectory() as temporary:
            loose = Path(temporary) / "front"
            loose.mkdir()

            self.assertIsNone(card_access.card_root_of(loose))


if __name__ == "__main__":
    unittest.main()
