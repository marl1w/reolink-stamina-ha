"""Where a clip lands in the cloud.

One job, kept separate because it is the part a user sees and therefore the part that must
not drift: a file named for the moment, the recorder and the camera, inside the folder the
syncer was configured with.

    Reolink/Main NVR/260804_084512_main-nvr_South Side.mp4

Both destinations seen so far reject the characters Windows rejects, and a clip whose name
came straight from a camera called "Front / Gate" would be refused on upload rather
than at configuration time — so names are sanitised here, once.
"""

from __future__ import annotations

import datetime as dt
import re

# Forbidden by OneDrive, SharePoint, and Windows itself. Note the colon: the obvious
# HH:MM:SS in a file name cannot survive an upload.
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# A name that ends in a dot or a space is silently mangled by Windows, and folder names
# with leading spaces sort strangely everywhere.
_TRIM = " ."


def safe_part(text: str, *, fallback: str = "unknown") -> str:
    """Make one path segment safe for every destination we support."""
    cleaned = _FORBIDDEN.sub("-", text).strip(_TRIM)
    # Collapse the runs of dashes that replacing several forbidden characters can leave.
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-" + _TRIM)
    # A segment of nothing but punctuation is legal and useless: "::" would survive as "-".
    if not any(character.isalnum() for character in cleaned):
        return fallback
    return cleaned


def clip_filename(when: dt.datetime, nvr: str, camera: str) -> str:
    """Name one clip after the moment it starts, its recorder and its camera.

    Date first, so a plain alphabetical listing is chronological — which is what makes the
    oldest clip easy to find when the quota is full, in any file browser as well as here.
    """
    stamp = when.strftime("%y%m%d_%H%M%S")
    return f"{stamp}_{safe_part(nvr, fallback='nvr')}_{safe_part(camera, fallback='camera')}.mp4"


def remote_path(folder: str, filename: str) -> str:
    """Join the configured folder and the file into a destination path.

    The folder may be several segments deep — the default is the root plus the recorder's
    name — and each is sanitised separately, so neither a slash inside a recorder's name
    nor a `..` in a hand-edited folder can write outside where the user pointed us.
    """
    parts = [safe_part(part) for part in folder.split("/") if part.strip(_TRIM)]
    return "/".join([*(parts or ["Reolink"]), filename])
