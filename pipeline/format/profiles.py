"""Platform format-compliance profiles.

Each profile is a named, config-driven spec (aspect ratio, max duration,
video/audio codec) describing what pipeline.format.validate checks a
staged output against. Adding a platform means adding an entry here, not
changing the validator.
"""

from __future__ import annotations

from typing import NamedTuple


class FormatProfile(NamedTuple):
    name: str
    aspect_ratio: tuple[int, int]
    max_duration_seconds: float
    video_codec: str
    audio_codec: str


PROFILES: dict[str, FormatProfile] = {
    "tiktok": FormatProfile(
        name="tiktok",
        aspect_ratio=(9, 16),
        max_duration_seconds=600.0,
        video_codec="h264",
        audio_codec="aac",
    ),
    "reels": FormatProfile(
        name="reels",
        aspect_ratio=(9, 16),
        max_duration_seconds=90.0,
        video_codec="h264",
        audio_codec="aac",
    ),
    "shorts": FormatProfile(
        name="shorts",
        aspect_ratio=(9, 16),
        max_duration_seconds=180.0,
        video_codec="h264",
        audio_codec="aac",
    ),
}
