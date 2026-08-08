"""The two closed vocabularies a clip is classified by."""

import unittest

from dashcam_exporter.domain import Channel, ClipMode


class VocabularyTest(unittest.TestCase):
    def test_channel_values_are_the_names_used_in_canonical_filenames(self):
        self.assertEqual(Channel.FRONT.value, "front")
        self.assertEqual(Channel.REAR.value, "rear")
        self.assertEqual(Channel.INTERIOR.value, "interior")
        self.assertEqual(Channel.TELEPHOTO.value, "telephoto")

    def test_modes_cover_the_four_cameras_researched(self):
        self.assertEqual(
            {mode.value for mode in ClipMode},
            {"normal", "event", "parking", "manual", "timelapse", "other"},
        )


if __name__ == "__main__":
    unittest.main()
