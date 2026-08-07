import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Cut:
    """A source clip interval; ``seconds=None`` means through its end."""

    trim_start: int = 0
    trim_seconds: int | None = None
    settled_at: int | None = None

    @property
    def start_seconds(self) -> int:
        return self.trim_start

    @property
    def seconds(self) -> int | None:
        return self.trim_seconds

    @property
    def settled_at_seconds(self) -> int | None:
        return self.settled_at

    def duration_for(self, clip_duration: int) -> int:
        return self.trim_seconds if self.trim_seconds is not None else clip_duration - self.trim_start

    def duration_of(self, clip: object) -> int:
        return self.duration_for(int(getattr(clip, "duration")))


@dataclass(frozen=True, slots=True)
class RenderOptions:
    """All rendering choices, passed as one immutable parameter object."""

    font_path: str
    use_videotoolbox: bool
    timestamp: bool = True
    speed: bool = True
    audio: bool = True
    output_height: int = 1080
    front_width: int = 2560
    front_height: int = 1600
    crop_top: int = 80
    crop_bottom: int = 80
    output_width: int = 1920
    fps: int = 30
    pip_width: int = 662
    pip_height: int = 372
    pip_margin: int = 24
    pip_position: str = "bottom-middle"
    watermark_text: str = "(c) Raoul Marc Schmidiger"
    watermark_size: int = 28
    watermark_position: str = "bottom-right"
    watermark_margin_h: int = 8
    watermark_margin_v: int = 6
    speed_font_size: int = 24
    speed_margin_v: int = 32
    speed_margin_r: int = 12
    map_panel_width: int = 480
    map_panel_position: str = "right"
    map_panel_gutter: int = 2
    vt_bitrate: str = "8M"
    vt_maxrate: str = "10M"
    x264_preset: str = "veryfast"
    x264_crf: str = "26"

    def __post_init__(self) -> None:
        if self.crop_top + self.crop_bottom >= self.front_height:
            raise ValueError("front crop must leave visible pixels")
        if self.pip_position not in {"bottom-middle", "top-left", "top-middle", "top-right"}:
            raise ValueError(f"unsupported PiP position: {self.pip_position}")
        if self.map_panel_position not in {"left", "right"}:
            raise ValueError(f"unsupported map panel position: {self.map_panel_position}")

    @property
    def with_timestamp(self) -> bool:
        return self.timestamp

    @property
    def with_speed(self) -> bool:
        return self.speed

    @property
    def no_audio(self) -> bool:
        return not self.audio

    def video_encoder(self) -> list[str]:
        if self.use_videotoolbox:
            return ["-c:v", "h264_videotoolbox", "-b:v", self._scale_bitrate(self.vt_bitrate),
                    "-maxrate", self._scale_bitrate(self.vt_maxrate), "-profile:v", "high"]
        return ["-c:v", "libx264", "-preset", self.x264_preset, "-crf", self.x264_crf]

    def _scale_bitrate(self, bitrate: str) -> str:
        if not self.output_height or self.output_height == 1080:
            return bitrate
        match = re.match(r"^(\d+(?:\.\d+)?)([KkMm]?)$", bitrate.strip())
        if not match:
            return bitrate
        value, unit = float(match.group(1)), match.group(2).upper()
        kbps = value * (1000 if unit == "M" else 1)
        scaled = max(500, int(round(kbps * (self.output_height / 1080) ** 2)))
        if scaled >= 1000:
            whole = scaled // 1000
            return f"{whole}M" if scaled % 1000 == 0 else f"{scaled / 1000:.1f}M"
        return f"{scaled}k"
