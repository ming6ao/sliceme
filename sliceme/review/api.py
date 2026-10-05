"""Action dispatch for the review surface, shared by HTTP and the CLI.

The HTTP route accepts only ``state``, ``diff``, ``file``, ``comment``,
``decision``, ``poll``, ``ack``, and ``deliver``.  Each handler validates its
parameters and calls :class:`sliceme.service.Service`, so the adapter stays thin
and every action stays agent-callable.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..util import SlicemeError

if TYPE_CHECKING:  # pragma: no cover
    from ..service import Service

__all__ = ["dispatch", "serve"]


def state(service: "Service", params: dict[str, Any]) -> dict[str, Any]:
    commit = _optional(params.get("commit"))
    return service.review_snapshot(commit=commit)


def diff(service: "Service", params: dict[str, Any]) -> dict[str, Any]:
    path = params.get("file")
    if not path:
        raise SlicemeError("review --diff requires --file")
    return service.review_diff(_optional(params.get("commit")), str(path))


def file(service: "Service", params: dict[str, Any]) -> dict[str, Any]:
    path = params.get("file")
    if not path:
        raise SlicemeError("review file requires --file")
    return service.review_file(_optional(params.get("commit")), str(path))


def comment(service: "Service", params: dict[str, Any]) -> dict[str, Any]:
    return service.review_comment(
        body=str(params.get("body") or ""),
        commit=_optional(params.get("commit")),
        file=_optional(params.get("file")),
        side=_optional(params.get("side")),
        line=_optional_int(params.get("line")),
        line_end=_optional_int(params.get("line_end")),
        node=_optional(params.get("node")),
    )


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


def poll(service: "Service", params: dict[str, Any]) -> dict[str, Any]:
    return service.review_poll()


def ack(service: "Service", params: dict[str, Any]) -> dict[str, Any]:
    comment_id = params.get("comment_id")
    if comment_id is None:
        raise SlicemeError("review --ack requires --comment-id")
    return service.review_ack(int(comment_id))


def deliver(service: "Service", params: dict[str, Any]) -> dict[str, Any]:
    return service.deliver(
        target=_optional(params.get("target")),
        source=_optional(params.get("source")),
    )


_HANDLERS = {
    "state": state,
    "diff": diff,
    "file": file,
    "comment": comment,
    "decision": decision,
    "report": report,
    "poll": poll,
    "ack": ack,
    "deliver": deliver,
}


def dispatch(service: "Service", action: str, params: dict[str, Any]) -> Any:
    handler = _HANDLERS.get(action)
    if handler is None:
        raise SlicemeError(f"unknown review action: {action}")
    return handler(service, params)


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def serve(
    plane_roots: list[Path],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    browser: bool = True,
    url_file: Path | str | None = None,
    campaign: str | None = None,
) -> dict[str, Any]:
    """Run the foreground review server.  Blocks until Ctrl-C."""
    from .server import run_server

    return run_server(
        plane_roots,
        host=host,
        port=port,
        browser=browser,
        url_file=url_file,
        campaign=campaign,
    )
