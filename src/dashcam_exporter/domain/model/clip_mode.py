from enum import Enum


class ClipMode(Enum):
    """Why the camera was recording, collapsed to what this tool decides on.

    BlackVue defines sixteen mode letters, VIOFO puts its marker in the
    filename, Thinkware puts it in the folder name and nowhere else. Clip
    keeps the vendor's own token alongside this, so collapsing here does not
    destroy evidence a later grouping rule may want.
    """

    NORMAL = "normal"
    EVENT = "event"
    PARKING = "parking"
    MANUAL = "manual"
    TIMELAPSE = "timelapse"
    OTHER = "other"
