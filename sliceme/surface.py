"""The one action surface, shared by every adapter.

The CLI and the pi extension both derive their verbs/tools from
:data:`ACTIONS`, so a surface can never exist in one adapter and not another.
:func:`dispatch` is the single implementation every adapter calls; adapters only
parse arguments and render results.

Names are deliberately few and overloaded by flags.  Every action is
agent-callable; there are no human-only actions.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .util import SlicemeError

if TYPE_CHECKING:  # pragma: no cover
    from .service import Service


@dataclass(frozen=True)
class Param:
    """One argument, rendered as a CLI flag and a tool property."""

    name: str
    type: str  # string | boolean | int | list
    help: str
    required: bool = False
    choices: tuple[str, ...] = ()
    flag: str | None = None  # CLI flag/stem; defaults to name with dashes


@dataclass(frozen=True)
class Action:
    name: str
    summary: str
    params: tuple[Param, ...] = ()
    aliases: tuple[str, ...] = ()


CAMPAIGN_HELP = (
    "campaign to operate on: a campaign branch, a branch key, or a unit name "
    "(default: the only campaign)"
)


ACTIONS: tuple[Action, ...] = (
    Action(
        name="start",
        summary="bootstrap the plane and a unit for this directory (idempotent)",
        aliases=("init",),
        params=(
            Param("name", "string", "unit name (default: slug of the directory name)"),
            Param("path", "string", "directory to bootstrap (default: cwd)"),
            Param("kind", "string", "unit kind", choices=("worker",)),
            Param("design", "string", "design document path; the campaign branch derives from its name"),
            Param("feature_branch", "string", "campaign branch (pull request head); default feat/<design-stem>"),
            Param("base", "string", "delivery base override (the pull request base; default the default branch)"),
            Param("checks", "list", "trusted check NAME=COMMAND (repeatable)", flag="check"),
            Param("force", "boolean", "overwrite an existing config"),
            Param("no_unit", "boolean", "initialise the plane without creating a unit for cwd"),
            Param("campaign", "string", CAMPAIGN_HELP),
        ),
    ),
    Action(
        name="status",
        summary="show units, candidates, waves, and health",
        params=(
            Param("unit", "string", "show one unit instead of the summary"),
            Param("short", "boolean", "print only the current unit name"),
            Param("dense", "boolean", "print the compact status summary (default)"),
            Param("verbose", "boolean", "print the full nested status dump"),
            Param("simulate", "boolean", "plan waves and verify the combined tree"),
            Param("health", "boolean", "check git/plane health"),
            Param("gc", "boolean", "prune worktrees and landed-unit branches"),
            Param("no_checks", "boolean", "with --simulate: plan only, do not run checks"),
            Param("sessions", "boolean", "list registered campaigns instead of the plane"),
            Param("resume", "boolean", "reconcile a suspended campaign and return its resume plan"),
            Param("plan_only", "boolean", "with --resume: accepted for compatibility; resume writes nothing"),
            Param("campaign", "string", CAMPAIGN_HELP),
        ),
    ),
    Action(
        name="ready",
        summary="current-wave nodes ready to spawn, the wave index, and paused",
        params=(Param("campaign", "string", CAMPAIGN_HELP),),
    ),
    Action(
        name="plan",
        summary="show the campaign split declared in a design document",
        params=(
            Param("design", "string", "design document path relative to the repo root"),
            Param("campaign", "string", CAMPAIGN_HELP),
        ),
    ),
    Action(
        name="deliver",
        summary="push the campaign worktree and open the delivery pull request (after approval)",
        params=(
            Param("source", "string", "campaign worktree branch (default: recorded worktree branch)"),
            Param("cleanup", "string", "cleanup after delivery", choices=("none", "worktrees", "all")),
            Param("no_checks", "boolean", "skip the plane's trusted checks"),
            Param("campaign", "string", CAMPAIGN_HELP),
        ),
    ),
    Action(
        name="check",
        summary="run the synchronous combined-tree checks for the current wave",
        params=(
            Param("current", "boolean", "run the checks for the current wave (the engine reads its own wave index)"),
            Param("campaign", "string", CAMPAIGN_HELP),
        ),
    ),
    Action(
        name="wave",
        summary="the campaign worktree: open it, or record a wave as per-node commits",
        params=(
            Param("open", "boolean", "create or reuse the single campaign worktree"),
            Param("record", "boolean", "record a wave: conformance + per-node commits"),
            Param("wave", "int", "wave index to record"),
            Param("current", "boolean", "record: the current wave (the engine reads its own wave index)"),
            Param("only", "list", "record: scope to these nodes, one commit each (repeatable)", flag="only"),
            Param("messages", "string", "record: JSON object of node id to description"),
            Param("summary", "string", "record: candidate summary"),
            Param("campaign", "string", CAMPAIGN_HELP),
        ),
    ),
    Action(
        name="review",
        summary="local review: record one campaign decision, or write the campaign report",
        params=(
            Param("decision", "string", "record a decision", choices=("approve", "request_changes", "override")),
            Param("all", "boolean", "with --decision approve: approve the whole campaign commit set"),
            Param("report", "boolean", "write the deterministic campaign report"),
            Param("narrative", "string", "report: what-changed/risks text"),
            Param("design", "string", "report: design document reference"),
            Param("commit", "string", "commit to approve"),
            Param("note", "string", "decision note"),
            Param("actor", "string", "who recorded the decision"),
            Param("campaign", "string", CAMPAIGN_HELP),
        ),
    ),
    Action(
        name="evidence",
        summary="write the deterministic evidence document (commits, checks, diffs, logs)",
        params=(
            Param("design", "string", "evidence: design document reference"),
            Param("campaign", "string", CAMPAIGN_HELP),
        ),
    ),
)

ACTION_BY_NAME: dict[str, Action] = {a.name: a for a in ACTIONS}
_ALIAS_TO_NAME: dict[str, str] = {alias: a.name for a in ACTIONS for alias in a.aliases}


def resolve_action(name: str) -> Action:
    canonical = _ALIAS_TO_NAME.get(name, name)
    action = ACTION_BY_NAME.get(canonical)
    if action is None:
        raise SlicemeError(f"unknown action: {name}")
    return action


def all_params() -> tuple[Param, ...]:
    """Union of parameters across actions, de-duplicated by name."""
    seen: dict[str, Param] = {}
    for action in ACTIONS:
        for param in action.params:
            seen.setdefault(param.name, param)
    return tuple(seen.values())


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
def dispatch(service: "Service", action: str, params: dict[str, Any]) -> Any:
    """Run *action* against *service* with adapter-neutral *params*."""
    spec = resolve_action(action)
    _validate(spec, params)
    if spec.name == "start":
        return start(params, cwd=service.root)
    handler = _HANDLERS[spec.name]
    return handler(service, params)


def start(params: dict[str, Any], *, cwd: str | Path | None = None) -> dict[str, Any]:
    """Bootstrap without an existing :class:`Service` (the plane may not exist)."""
    from .service import Service

    path = params.get("path") or cwd or os.getcwd()
    return Service.init(
        path,
        name=params.get("name"),
        design=params.get("design"),
        feature_branch=params.get("feature_branch"),
        base=params.get("base"),
        kind=params.get("kind") or "worker",
        checks=parse_checks(params.get("checks") or []),
        force=bool(params.get("force")),
        no_unit=bool(params.get("no_unit")),
    )


def parse_checks(items: list[str]) -> list[dict[str, Any]]:
    checks = []
    for item in items:
        if "=" in item:
            name, command = item.split("=", 1)
            checks.append({"name": name.strip(), "command": command.strip(), "required": True})
        else:
            checks.append({"name": item, "command": item, "required": True})
    return checks


def doctor(root: Path) -> dict[str, Any]:
    from . import gitutil

    checks = [
        ("git", gitutil.is_git_repo(root)),
        ("config", (root / ".sliceme" / "config.json").is_file()),
        ("state_db", (root / ".sliceme" / "state.db").is_file()),
    ]
    return {
        "root": str(root),
        "checks": [{"name": name, "ok": ok} for name, ok in checks],
        "ok": all(ok for _, ok in checks),
    }


def _dispatch_status(service: "Service", p: dict[str, Any]) -> Any:
    if p.get("gc"):
        return service.gc(artifacts=True)
    if p.get("health"):
        return doctor(service.root)
    if p.get("simulate"):
        return service.simulation(run_checks_flag=not p.get("no_checks"))
    if p.get("sessions"):
        return service.sessions()
    if p.get("resume"):
        return service.resume(plan_only=bool(p.get("plan_only")))
    if p.get("short"):
        return {"unit": service.current_unit()["name"]}
    if p.get("unit"):
        return service.unit_detail(p["unit"])
    if p.get("verbose"):
        return service.status()
    if p.get("dense"):
        return service.status_summary()
    if p.get("json"):
        return service.status()
    return service.status_summary()


def _dispatch_plan(service: "Service", p: dict[str, Any]) -> Any:
    design = p.get("design")
    if not design:
        raise SlicemeError("plan requires --design")
    return service.campaign_plan(str(design))


def _dispatch_ready(service: "Service", p: dict[str, Any]) -> Any:
    return service.ready()


def _dispatch_deliver(service: "Service", p: dict[str, Any]) -> Any:
    return service.deliver(
        source=p.get("source"),
        cleanup=p.get("cleanup") or "none",
        run_checks_flag=not p.get("no_checks"),
    )


def _dispatch_check(service: "Service", p: dict[str, Any]) -> Any:
    if not p.get("current"):
        raise SlicemeError("check requires --current")
    return service.check_wave()


def _dispatch_wave(service: "Service", p: dict[str, Any]) -> Any:
    if p.get("open"):
        return {"unit": service.create_campaign_workspace()}
    if p.get("record"):
        if p.get("current"):
            wave_index = service.current_wave_index()
        elif p.get("wave") is not None:
            wave_index = int(p["wave"])
        else:
            raise SlicemeError("wave --record requires --wave or --current")
        messages = p.get("messages")
        if isinstance(messages, str):
            try:
                messages = json.loads(messages) if messages.strip() else None
            except json.JSONDecodeError as exc:
                raise SlicemeError(
                    f"wave --record --messages is not valid JSON: {exc}"
                ) from None
        if messages is not None and not isinstance(messages, dict):
            raise SlicemeError("wave --record --messages must be a JSON object")
        only = list(p.get("only") or []) or None
        # Serialize git mutation with campaign creation and wave recording.
        from .service import campaign_lock

        with campaign_lock(service.root):
            return service.record_wave(
                wave_index,
                only=only,
                messages=messages,
                summary=p.get("summary"),
            )
    raise SlicemeError("wave needs --open or --record")


def _dispatch_review(service: "Service", p: dict[str, Any]) -> Any:
    from .review import api as review_api

    if p.get("decision"):
        return review_api.decision(service, p)
    if p.get("report"):
        return review_api.report(service, p)
    raise SlicemeError("review needs --decision or --report")


def _dispatch_evidence(service: "Service", p: dict[str, Any]) -> Any:
    return service.evidence(design=p.get("design"))


_HANDLERS = {
    "status": _dispatch_status,
    "ready": _dispatch_ready,
    "plan": _dispatch_plan,
    "deliver": _dispatch_deliver,
    "check": _dispatch_check,
    "wave": _dispatch_wave,
    "review": _dispatch_review,
    "evidence": _dispatch_evidence,
}


def _validate(spec: Action, params: dict[str, Any]) -> None:
    for param in spec.params:
        if param.required and not params.get(param.name):
            raise SlicemeError(f"{spec.name} requires --{param.name.replace('_', '-')}")
        value = params.get(param.name)
        if value and param.choices and value not in param.choices:
            raise SlicemeError(
                f"{spec.name} --{param.name.replace('_', '-')} must be one of: "
                + ", ".join(param.choices)
            )
