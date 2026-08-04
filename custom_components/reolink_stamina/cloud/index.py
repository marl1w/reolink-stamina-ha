"""What has been uploaded, and what has to go to make room.

The quota is enforced against this index rather than by asking the destination how full it
is, for two reasons: a listing costs a round trip on every upload, and the folder may hold
files this integration did not put there — a recorder's 15 GB should mean 15 GB of *its* clips,
not 15 GB minus whatever else lives in the same drive.

The index is therefore the record of the clips we own, reconciled against the destination
when a syncer starts so that files deleted by hand do not count forever.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt


@dataclass(slots=True, frozen=True)
class StoredClip:
    """One clip already in the cloud."""

    path: str
    size: int
    # When the footage was recorded, not when it was uploaded: eviction should retire the
    # oldest *event*, which is what a person means by "the oldest clip", even if a retry
    # uploaded it out of order.
    recorded: dt.datetime

    def as_dict(self) -> dict[str, object]:
        """Serialise for storage."""
        return {"path": self.path, "size": self.size, "recorded": self.recorded.isoformat()}

    @classmethod
    def from_dict(cls, data: dict) -> StoredClip:
        """Restore from storage."""
        return cls(
            path=str(data["path"]),
            size=int(data["size"]),
            recorded=dt.datetime.fromisoformat(data["recorded"]),
        )


class ClipIndex:
    """The clips one syncer owns, oldest first."""

    def __init__(self, clips: list[StoredClip] | None = None) -> None:
        """Initialise, keeping the invariant that the list is ordered oldest first."""
        self._clips: list[StoredClip] = sorted(clips or [], key=lambda clip: clip.recorded)

    def __len__(self) -> int:
        """Return the number of clips held."""
        return len(self._clips)

    @property
    def used(self) -> int:
        """Return the bytes occupied."""
        return sum(clip.size for clip in self._clips)

    def add(self, clip: StoredClip) -> None:
        """Record an uploaded clip, keeping the order."""
        self._clips = sorted([*self._clips, clip], key=lambda item: item.recorded)

    def remove(self, path: str) -> None:
        """Forget a clip, whether we deleted it or someone else did."""
        self._clips = [clip for clip in self._clips if clip.path != path]

    def plan_eviction(self, incoming: int, quota: int) -> list[StoredClip]:
        """Which clips must go for `incoming` bytes to fit inside `quota`.

        Oldest first, and no further than needed. Returns *nothing* when the clip could not fit
        even in an empty store: evicting in that case would empty the archive and still fail,
        so the caller is left to see that it does not fit and say so.
        """
        if incoming > quota:
            return []

        doomed: list[StoredClip] = []
        used = self.used
        for clip in self._clips:
            if used + incoming <= quota:
                break
            doomed.append(clip)
            used -= clip.size
        return doomed

    def reconcile(self, present: set[str]) -> list[StoredClip]:
        """Drop clips the destination no longer has, returning what was forgotten.

        Someone tidying the folder by hand should free that space here too, or the quota
        would slowly starve the syncer of room it actually has.
        """
        missing = [clip for clip in self._clips if clip.path not in present]
        if missing:
            self._clips = [clip for clip in self._clips if clip.path in present]
        return missing

    def as_list(self) -> list[dict[str, object]]:
        """Serialise for storage."""
        return [clip.as_dict() for clip in self._clips]

    @classmethod
    def from_list(cls, data: list | None) -> ClipIndex:
        """Restore from storage, ignoring entries too damaged to read."""
        clips = []
        for item in data or []:
            try:
                clips.append(StoredClip.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
        return cls(clips)
