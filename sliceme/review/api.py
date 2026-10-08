"""The two review actions, shared by the CLI.

The reduced review surface keeps two actions.  ``decision`` records one
campaign approval.  ``report`` renders the deterministic campaign report.  Each
handler validates its parameters and calls :class:`sliceme.service.Service`, so
the adapter stays thin and every action stays agent-callable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..util import SlicemeError

if TYPE_CHECKING:  # pragma: no cover
    from ..service import Service

__all__ = ["decision", "report"]


def decision(service: "Service", params: dict[str, Any]) -> dict[str, Any]:
    action = params.get("decision") or params.get("action")
    if not action:
        raise SlicemeError("review decision requires an action")
    return service.review_decision(
        action=str(action),
        commit=_optional(params.get("commit")),
        all_commits=bool(params.get("all")),
        actor=_optional(params.get("actor")),
        note=_optional(params.get("note")),
    )


def report(service: "Service", params: dict[str, Any]) -> dict[str, Any]:
    return service.report(
        narrative=_optional(params.get("narrative")),
        design=_optional(params.get("design")),
    )


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
