import io
import unittest

from faster_whisper_transcriber import TranscriptSegment
from speaker_diarizer import SpeakerDiarizer, SpeakerLabeler, SpeakerTurn


class SpeakerDiarizerTest(unittest.TestCase):
    def test_reads_and_sorts_pyannote_speaker_turns(self) -> None:
        class Turn:
            def __init__(self, start: float, end: float) -> None:
                self.start = start
                self.end = end

        class Annotation:
            def itertracks(self, yield_label: bool = False):
                self.assert_true(yield_label)
                yield Turn(2.0, 3.0), None, "SPEAKER_01"
                yield Turn(0.0, 2.0), None, "SPEAKER_00"

            @staticmethod
            def assert_true(value: bool) -> None:
                assert value

        diarizer = SpeakerDiarizer(
            pipeline_factory=lambda _model, _token: lambda _audio: Annotation()
        )
        turns = diarizer.diarizeMp3(io.BytesIO(b"audio"))

        self.assertEqual("SPEAKER_00", turns[0].speaker)
        self.assertEqual(2.0, turns[1].start_seconds)

    def test_labels_segment_using_total_speaker_overlap(self) -> None:
        labeler = SpeakerLabeler(
            [
                SpeakerTurn(0.0, 1.0, "A"),
                SpeakerTurn(1.0, 3.0, "B"),
                SpeakerTurn(3.0, 3.5, "A"),
            ]
        )

        labeled = labeler.label(TranscriptSegment(0.5, 3.5, "Hello"))

        self.assertEqual("B", labeled.speaker)

    def test_leaves_segment_unlabeled_when_there_is_no_overlap(self) -> None:
        labeler = SpeakerLabeler([SpeakerTurn(5.0, 6.0, "A")])
        segment = TranscriptSegment(0.0, 1.0, "Hello")

        self.assertIs(segment, labeler.label(segment))
