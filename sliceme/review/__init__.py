"""Local review: a loopback server, a review packet, and the approval pin.

The package is standard-library only.  ``server.py`` owns HTTP, ``api.py`` owns
the action dispatch shared by HTTP and the CLI, ``packet.py`` builds one review
snapshot, ``diff.py`` parses git diffs, and ``security.py`` owns the loopback
boundary and the write token.

Every state write goes through :class:`sliceme.service.Service`; the HTTP
adapter never touches the store or git directly.
"""

from __future__ import annotations

__all__ = ["api", "diff", "packet", "security"]
