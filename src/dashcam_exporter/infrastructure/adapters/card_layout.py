from abc import ABC, abstractmethod
from pathlib import Path

from dashcam_exporter.domain import Clip, Track


class CardLayout(ABC):
    """Everything this tool asks about one specific card.

    Five questions, and they are the pipeline's questions rather than any
    camera's answers. A camera that stores its track as an s-expression, in a
    mislabeled tar, or inside the video container satisfies track_for the same
    way: by returning our Track. Nothing camera-shaped travels past here.
    """

    @abstractmethod
    def clips(self) -> list[Clip]:
        """Every clip on the card, in ascending recording-time order.

        Channels are paired here, by whatever rule the camera requires --
        exact timestamps for Thinkware, a tolerance window for DDPAI and
        VIOFO, whose front and rear clocks drift apart.
        """

    @abstractmethod
    def stamp_of(self, path: Path) -> str | None:
        """This file's canonical YYYYMMDDHHMMSS stamp, or None if it has none.

        The canonical form is ours. A camera writing 2020_1018_170010 or
        REC_2019_07_01_10_25_30_F translates here and nowhere else.
        """

    @abstractmethod
    def track_for(self, clip: Clip) -> Track | None:
        """The parsed track covering this clip, or None if it recorded none."""

    @abstractmethod
    def import_roots(self) -> tuple[Path, ...]:
        """Directories the importer copies, as absolute paths.

        Cards hoard: DDPAI keeps photos, thumbnails and logs this tool never
        reads, and copying them costs gigabytes per import.
        """

    @abstractmethod
    def is_track_artifact(self, path: Path) -> bool:
        """Whether this file carries GPS, however the camera stores it.

        The destructive paths ask before they erase, so a wrong answer here
        deletes a drive's route.
        """
