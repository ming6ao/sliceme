"""Local review: the review packet and the approval pin.

The package is standard-library only.  ``api.py`` owns the two remaining review
actions, ``packet.py`` builds one review snapshot, and ``diff.py`` parses git
diffs.

Every state write goes through :class:`sliceme.service.Service`.
"""

from __future__ import annotations

__all__ = ["api", "diff", "packet"]
