from enum import Enum


class Channel(Enum):
    """Which camera a video file came from.

    Front and rear were enough until the VIOFO A139 Pro, which is three
    channels sharing one timestamp, and the A329, which adds a telephoto.
    """

    FRONT = "front"
    REAR = "rear"
    INTERIOR = "interior"
    TELEPHOTO = "telephoto"
