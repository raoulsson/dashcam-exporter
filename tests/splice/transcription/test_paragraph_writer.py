import io
import json
import tempfile
import unittest
from pathlib import Path

from faster_whisper_transcriber import TranscriptSegment
from paragraph_writer import ParagraphWriter


class ParagraphWriterTest(unittest.TestCase):
    def test_flushes_after_a_sentence_when_the_paragraph_is_long_enough(self) -> None:
        destination = io.StringIO()
        writer = ParagraphWriter(destination, minimum_characters=30, maximum_characters=100)

        writer.write_segment(TranscriptSegment(0.0, 1.0, "First complete sentence."))
        writer.write_segment(TranscriptSegment(1.1, 2.0, "Second sentence."))
        writer.write_segment(TranscriptSegment(2.1, 3.0, "Third sentence."))
        writer.close()

        self.assertEqual(
            "First complete sentence. Second sentence.\n\nThird sentence.\n\n",
            destination.getvalue(),
        )

    def test_flushes_on_a_significant_pause_even_for_short_text(self) -> None:
        destination = io.StringIO()
        writer = ParagraphWriter(destination, pause_seconds=1.5)

        writer.write_segment(TranscriptSegment(0.0, 1.0, "A short phrase"))
        writer.write_segment(TranscriptSegment(3.0, 4.0, "A new thought."))
        writer.close()

        self.assertEqual("A short phrase\n\nA new thought.\n\n", destination.getvalue())

    def test_rejects_an_invalid_paragraph_size_range(self) -> None:
        with self.assertRaises(ValueError):
            ParagraphWriter(io.StringIO(), minimum_characters=701, maximum_characters=700)

    def test_speaker_change_starts_a_labeled_paragraph(self) -> None:
        destination = io.StringIO()
        writer = ParagraphWriter(destination)
        writer.write_segment(TranscriptSegment(0.0, 1.0, "Hello.", "SPEAKER_00"))
        writer.write_segment(TranscriptSegment(1.0, 2.0, "Hi.", "SPEAKER_01"))
        writer.close()

        self.assertEqual(
            "SPEAKER_00: Hello.\n\nSPEAKER_01: Hi.\n\n", destination.getvalue()
        )

        with tempfile.TemporaryDirectory() as directory:
            timeline_path = Path(directory) / "timeline.json"
            writer.write_timeline(timeline_path)
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        self.assertEqual("SPEAKER_00", timeline["paragraphs"][0]["speaker"])
        self.assertEqual("SPEAKER_01", timeline["paragraphs"][1]["speaker"])

    def test_writes_a_timeline_with_media_and_text_offsets(self) -> None:
        destination = io.StringIO()
        writer = ParagraphWriter(destination, pause_seconds=1.5)
        writer.write_segment(TranscriptSegment(2.5, 3.0, "First paragraph."))
        writer.write_segment(TranscriptSegment(5.0, 6.0, "Second paragraph."))
        writer.close()

        with tempfile.TemporaryDirectory() as directory:
            timeline_path = Path(directory) / "timeline.json"
            writer.write_timeline(timeline_path)
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))

        self.assertEqual("paragraph-timeline/v1", timeline["format"])
        self.assertEqual(
            {
                "paragraph_index": 0,
                "start_seconds": 2.5,
                "end_seconds": 3.0,
                "start_character": 0,
                "end_character": 16,
            },
            timeline["paragraphs"][0],
        )
        self.assertEqual(18, timeline["paragraphs"][1]["start_character"])
