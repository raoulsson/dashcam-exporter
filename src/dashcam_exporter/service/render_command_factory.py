from pathlib import Path

from dashcam_exporter.domain import Cut, Clip, RenderOptions
from dashcam_exporter.media import FilterGraphFactory


class RenderCommandFactory:
    """Creates a complete, inspectable FFmpeg invocation for one clip."""

    def __init__(self, filter_graph_factory: FilterGraphFactory) -> None:
        self._filter_graph_factory = filter_graph_factory

    def create(self, clip: Clip, output: Path, options: RenderOptions, cut: Cut,
               speed_srt: Path | None = None, map_video: Path | None = None) -> list[str]:
        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"]
        if cut.start_seconds:
            command += ["-ss", str(cut.start_seconds)]
        command += ["-i", str(clip.front)]
        if clip.rear is not None:
            command += ["-i", str(clip.rear)]
        if map_video is not None:
            command += ["-stream_loop", "-1", "-i", str(map_video)]
        command += ["-t", str(cut.duration_for(clip.duration))]
        graph = self._filter_graph_factory.create(
            options, clip.epoch_utc + cut.start_seconds, speed_srt, map_video is not None, clip.rear is not None,
        )
        command += ["-filter_complex", graph, "-map", "[out]"]
        if not options.audio:
            command.append("-an")
        elif clip.rear is not None:
            command += ["-map", "0:a?"]
        command += self._encoder_args(options)
        return command + ["-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)]

    @staticmethod
    def _encoder_args(options: RenderOptions) -> list[str]:
        if options.use_videotoolbox:
            return ["-c:v", "h264_videotoolbox", "-b:v", options.vt_bitrate,
                    "-maxrate", options.vt_maxrate, "-profile:v", "high"]
        return ["-c:v", "libx264", "-preset", options.x264_preset, "-crf", options.x264_crf]
