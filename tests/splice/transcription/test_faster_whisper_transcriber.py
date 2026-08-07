import io
import unittest

from faster_whisper_transcriber import FasterWhisperTranscriber


class FasterWhisperTranscriberTest(unittest.TestCase):
    def test_transcribe_mp3_returns_structured_segments_and_text(self) -> None:
        received_paths: list[str] = []

        class Segment:
            def __init__(self, start: float, end: float, text: str) -> None:
                self.start = start
                self.end = end
                self.text = text

        class Info:
            language = "en"
            duration = 2.0

        class FakeModel:
            def transcribe(self, audio: str, **_: object) -> tuple[list[Segment], Info]:
                received_paths.append(audio)
                return [Segment(0.0, 1.2, " Hello"), Segment(1.2, 2.0, "world.")], Info()

        transcription = FasterWhisperTranscriber(
            model_factory=lambda *_args, **_kwargs: FakeModel()
        ).transcribeMp3(io.BytesIO(b"fake mp3"))

        self.assertEqual("en", transcription.language)
        self.assertEqual("Hello world.", transcription.text)
        self.assertEqual(2, len(transcription.segments))
        self.assertEqual(0.0, transcription.segments[0].start_seconds)
        self.assertEqual(1.2, transcription.segments[0].end_seconds)
        self.assertEqual(1, len(received_paths))

    def test_transcribe_mp3_reports_loading_and_audio_progress(self) -> None:
        updates: list[float] = []

        class Segment:
            start = 0.0
            end = 5.0
            text = "A segment."

        class Info:
            language = "en"
            duration = 10.0

        class FakeModel:
            def transcribe(self, _audio: str, **_kwargs: object) -> tuple[list[Segment], Info]:
                return [Segment()], Info()

        FasterWhisperTranscriber(
            model_factory=lambda *_args, **_kwargs: FakeModel()
        ).transcribeMp3(io.BytesIO(b"fake mp3"), updates.append)

        self.assertEqual([0.0, 5.0, 10.0, 55.0, 100.0], updates)

    def test_transcribe_mp3_can_stream_segments_without_retaining_them(self) -> None:
        received_segments: list[str] = []

        class Segment:
            start = 0.0
            end = 1.0
            text = " Streamed text."

        class Info:
            language = "en"
            duration = 1.0

        class FakeModel:
            def transcribe(self, _audio: str, **_kwargs: object) -> tuple[list[Segment], Info]:
                return [Segment()], Info()

        transcription = FasterWhisperTranscriber(
            model_factory=lambda *_args, **_kwargs: FakeModel()
        ).transcribeMp3(
            io.BytesIO(b"fake mp3"),
            segment_callback=lambda segment: received_segments.append(segment.text),
            retain_segments=False,
        )

        self.assertEqual([" Streamed text."], received_segments)
        self.assertEqual((), transcription.segments)
