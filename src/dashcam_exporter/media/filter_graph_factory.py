from pathlib import Path

from dashcam_exporter.domain import RenderOptions


class FilterGraphFactory:
    """Builds the FFmpeg filter graph from an immutable render parameter object."""

    def create(self, options: RenderOptions, start_epoch: int, speed_srt: Path | None,
               with_map_widget: bool, with_rear: bool) -> str:
        graph = self._front_graph(options)
        graph += self._rear_graph(options) if with_rear else "[front]null"
        if options.timestamp:
            graph += self._timestamp_graph(options, start_epoch)
        if speed_srt is not None:
            graph += self._speed_graph(options, speed_srt)
        graph += self._watermark_graph(options)
        return graph + (self._map_graph(options, with_rear) if with_map_widget else "[out]")

    @staticmethod
    def _front_graph(options: RenderOptions) -> str:
        height = options.front_height - options.crop_top - options.crop_bottom
        return (f"[0:v]crop={options.front_width}:{height}:0:{options.crop_top},"
                f"scale={options.output_width}:1080,setsar=1,fps={options.fps}[front];")

    @staticmethod
    def _rear_graph(options: RenderOptions) -> str:
        margin = options.pip_margin
        x = {"bottom-middle": "(W-w)/2", "top-left": str(margin),
             "top-middle": "(W-w)/2", "top-right": f"W-w-{margin}"}[options.pip_position]
        y = f"H-h-{margin}" if options.pip_position == "bottom-middle" else str(margin)
        return (f"[1:v]scale={options.pip_width}:{options.pip_height},setsar=1,fps={options.fps},"
                f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.9:t=3[rear];[front][rear]overlay={x}:{y}")

    @staticmethod
    def _timestamp_graph(options: RenderOptions, start_epoch: int) -> str:
        font = options.font_path.replace(":", r"\:")
        return (f",drawtext=fontfile={font}:text='%{{pts\\:gmtime\\:{start_epoch}\\:%Y-%m-%d %T}}':"
                "fontcolor=white:fontsize=36:box=1:boxcolor=black@0.55:boxborderw=10:x=24:y=h-th-24")

    @staticmethod
    def _speed_graph(options: RenderOptions, speed_srt: Path) -> str:
        style = (f"Alignment=3,FontName=Courier New,FontSize={options.speed_font_size},"
                 "PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BackColour=&H80000000,"
                 f"BorderStyle=4,Outline=2,Shadow=0,MarginV={options.speed_margin_v},MarginR={options.speed_margin_r}")
        return f",subtitles=filename='{speed_srt.as_posix()}':force_style='{style}'"

    @staticmethod
    def _watermark_graph(options: RenderOptions) -> str:
        if not options.font_path or not options.watermark_text:
            return ""
        x, y = {"bottom-left": (str(options.watermark_margin_h), f"h-th-{options.watermark_margin_v}"),
                "top-right": (f"w-tw-{options.watermark_margin_h}", str(options.watermark_margin_v)),
                "top-left": (str(options.watermark_margin_h), str(options.watermark_margin_v)),
                "bottom-right": (f"w-tw-{options.watermark_margin_h}", f"h-th-{options.watermark_margin_v}")}[options.watermark_position]
        text = options.watermark_text.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'")
        font = options.font_path.replace(":", r"\:")
        return (f",drawtext=fontfile={font}:text='{text}':fontcolor=white@0.85:"
                f"fontsize={options.watermark_size}:borderw=2:bordercolor=black@0.6:x={x}:y={y}")

    @staticmethod
    def _map_graph(options: RenderOptions, with_rear: bool) -> str:
        map_input = "[2:v]" if with_rear else "[1:v]"
        gutter = options.map_panel_gutter
        left = options.map_panel_position == "left"
        pad_x = gutter if not left else 0
        graph = (f"[video_part];{map_input}scale={options.map_panel_width}:1080,setsar=1,fps={options.fps},"
                 f"pad={options.map_panel_width + gutter}:1080:{pad_x}:0:color=black[map_part];")
        return graph + ("[map_part][video_part]hstack[out]" if left else "[video_part][map_part]hstack[out]")
