"""Undoing a camera's filing system, on a copy that is already safe."""

import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from dashcam_exporter.application.workflow import card_access
from dashcam_exporter.application.workflow.canonical_workspace import (
    CanonicalWorkspace)
from dashcam_exporter.application.workflow.normalizer import Normalizer
from dashcam_exporter.domain import Channel

NMEA = (
    "$GPRMC,090530.00,A,4712.3456,N,00832.1234,E,20.0,35.3,060826,,,A*52\n"
    "$GPRMC,090531.00,A,4712.4000,N,00832.2000,E,30.0,35.3,060826,,,A*52\n"
)


def build_import(root: Path) -> Path:
    """A DDPAI import as rsync leaves it: the card's tree, verbatim."""
    video = root / "DCIM/200video"
    (video / "front").mkdir(parents=True)
    (video / "rear").mkdir(parents=True)
    (root / "DCIM/201photo/front").mkdir(parents=True)
    tar_directory = root / "DCIM/203gps/tar"
    tar_directory.mkdir(parents=True)

    (video / "front/20260806170529_0060.mp4").write_bytes(b"front bytes")
    (video / "rear/20260806170530_0060_A.mp4").write_bytes(b"rear bytes")
    (root / "DCIM/201photo/front/20260806170529_0060.jpg").write_bytes(b"jpg")
    (root / "DCIM/IPSRecord.txt").write_text("drive log")

    payload = NMEA.encode()
    info = tarfile.TarInfo("20260806170529_0060.gpx")
    info.size = len(payload)
    with tarfile.open(tar_directory / "20260806170529_0540.git", "w") as handle:
        handle.addfile(info, io.BytesIO(payload))
    return root


class NormalizerTest(unittest.TestCase):
    def setUp(self):
        card_access.forget()

    def test_a_dry_run_writes_absolutely_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_import(Path(temporary))
            before = sorted(p.relative_to(root).as_posix()
                            for p in root.rglob("*"))

            plan = Normalizer(root).plan()

            after = sorted(p.relative_to(root).as_posix()
                           for p in root.rglob("*"))

        self.assertEqual(before, after)
        self.assertEqual(plan.adapter, "ddpai")
        self.assertEqual(plan.clips, 1)
        self.assertEqual(plan.moves, 2)
        self.assertEqual(plan.tracks, 1)

    def test_videos_are_moved_into_canonical_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_import(Path(temporary))

            Normalizer(root).apply()
            workspace = CanonicalWorkspace(root)
            names = sorted(p.name for p in workspace.clips_dir.iterdir())
            old_front = root / "DCIM/200video/front/20260806170529_0060.mp4"

        self.assertEqual(names, ["20260806170529_front.mp4",
                                 "20260806170529_rear.mp4"])
        self.assertFalse(old_front.exists(), "the video was moved, not copied")

    def test_the_rear_video_is_named_for_the_clip_not_for_its_own_clock(self):
        # The rear camera stamps a second late. Canonical names come from the
        # clip, so the pair stops disagreeing the moment normalisation runs.
        with tempfile.TemporaryDirectory() as temporary:
            root = build_import(Path(temporary))

            Normalizer(root).apply()
            rear = CanonicalWorkspace(root).clips()[0].videos[Channel.REAR]

        self.assertEqual(rear.name, "20260806170529_rear.mp4")

    def test_images_and_logs_are_copied_and_the_originals_stay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_import(Path(temporary))

            Normalizer(root).apply()
            workspace = CanonicalWorkspace(root)
            images = sorted(p.name for p in workspace.images_dir.iterdir())
            logs = sorted(p.name for p in workspace.logs_dir.iterdir())
            original_log_survives = (root / "DCIM/IPSRecord.txt").is_file()

        self.assertEqual(images, ["20260806170529.jpg"])
        self.assertEqual(logs, ["IPSRecord.txt"])
        self.assertTrue(original_log_survives)

    def test_the_camera_gps_becomes_our_own_track_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_import(Path(temporary))

            Normalizer(root).apply()
            workspace = CanonicalWorkspace(root)
            clip = workspace.clips()[0]
            track = workspace.track_for(clip)
            written = sorted(p.name for p in workspace.tracks_dir.iterdir())

        self.assertEqual(written, ["20260806170529.json"])
        self.assertIsNotNone(track)
        self.assertEqual(len(track.points), 2)

    def test_the_manifest_survives_the_rename_with_what_names_cannot_hold(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = build_import(Path(temporary))

            Normalizer(root).apply()
            clip = CanonicalWorkspace(root).clips()[0]

        self.assertEqual(clip.timestamp, "20260806170529")
        self.assertEqual(clip.wall_seconds, 60)
        self.assertEqual(clip.playback_seconds, 60)
        self.assertFalse(clip.protected)
        self.assertEqual(sorted(c.value for c in clip.videos),
                         ["front", "rear"])

    def test_running_it_twice_moves_nothing_the_second_time(self):
        # The operator will run this again. A second pass must be a no-op,
        # not a second set of renames over half-moved footage.
        with tempfile.TemporaryDirectory() as temporary:
            root = build_import(Path(temporary))

            Normalizer(root).apply()
            card_access.forget()
            second = Normalizer(root).apply()

        self.assertEqual(second.moves, 0)
        self.assertEqual(second.tracks, 0)
        self.assertEqual(second.copies, 0)

    def test_a_tree_no_adapter_claims_is_left_alone_entirely(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "DCIM/100CANON").mkdir(parents=True)
            (root / "DCIM/100CANON/IMG_0001.jpg").write_bytes(b"canon")

            plan = Normalizer(root).apply()
            untouched = (root / "DCIM/100CANON/IMG_0001.jpg").is_file()
            made_nothing = not (root / "clips").exists()

        self.assertEqual(plan.adapter, "none")
        self.assertTrue(plan.is_noop)
        self.assertTrue(untouched)
        self.assertTrue(made_nothing)


if __name__ == "__main__":
    unittest.main()
