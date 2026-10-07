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

from .sandbox import SANDBOX_MODES
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
    "campaign to operate on: a target branch, a branch key, or a unit name "
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
            Param("base", "string", "base branch/ref for new worktrees"),
            Param("target", "string", "target (feature) branch delivery lands on; never main/master", flag="target"),
            Param("target_mode", "string", "how to resolve --target", choices=("current", "existing", "new")),
            Param("worktree_branch", "string", "campaign accumulation branch (default derived)"),
            Param("main_branch", "string", "deprecated alias for --target", flag="main"),
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
            Param("simulate", "boolean", "plan waves and verify the combined tree"),
            Param("health", "boolean", "check git/plane health"),
            Param("gc", "boolean", "prune worktrees and landed-unit branches"),
            Param("no_checks", "boolean", "with --simulate: plan only, do not run checks"),
            Param("sessions", "boolean", "list registered campaigns instead of the plane"),
            Param("resume", "boolean", "reconcile a suspended campaign and return its resume plan"),
            Param("plan_only", "boolean", "with --resume: report without side effects"),
            Param("campaign", "string", CAMPAIGN_HELP),
        ),
    ),
    Action(
        name="deliver",
        summary="push the campaign worktree and open the delivery pull request (after approval)",
        params=(
            Param("target", "string", "target feature branch for the pull request (default: recorded target)"),
            Param("source", "string", "campaign worktree branch (default: recorded worktree branch)"),
            Param("cleanup", "string", "cleanup after delivery", choices=("none", "worktrees", "all")),
            Param("no_checks", "boolean", "skip the plane's trusted checks"),
            Param("campaign", "string", CAMPAIGN_HELP),
        ),
    ),
    Action(
        name="exec",
        summary="single sandboxed verification executor: submit/run/wait/cancel check jobs",
        params=(
            Param("submit", "boolean", "enqueue a check job"),
            Param("validate", "boolean", "resolve and validate the project sandbox gate"),
            Param("gpu_required", "boolean", "with --validate: require a GPU runner"),
            Param("run", "boolean", "drain the queue with the single executor"),
            Param("wait", "boolean", "wait for a job to finish (requires --job)"),
            Param("cancel", "boolean", "cancel a queued job (requires --job)"),
            Param("job", "string", "job id for --wait/--cancel"),
            Param("source", "string", "fingerprint source, e.g. node:w1 or wave:0"),
            Param("commit", "string", "commit/ref to run the checks at"),
            Param("commits", "list", "submit: commit refs run as one batch (repeatable)", flag="commits"),
            Param("only", "list", "submit: keep only these checks, matched by name or command", flag="only"),
            Param("command", "list", "check command (repeatable)", flag="command"),
            Param("sandbox", "string", "sandbox mode", choices=SANDBOX_MODES),
            Param("gpu", "string", "GPU tier reserved by the executor", choices=("none", "T1", "T2")),
            Param("priority", "int", "higher priority runs first"),
            Param("timeout", "int", "per-command timeout seconds"),
            Param("wave", "int", "campaign wave the job belongs to"),
            Param("requester", "string", "verifier id that submitted the job"),
            Param("limit", "int", "with --run: at most this many jobs"),
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
            Param("only", "list", "record: scope to these nodes, one commit each (repeatable)", flag="only"),
            Param("messages", "string", "record: JSON object of node id to description"),
            Param("summary", "string", "record: candidate summary"),
            Param("campaign", "string", CAMPAIGN_HELP),
        ),
    ),
    Action(
        name="review",
        summary="local review: serve the browser client, poll comments, or record one review action",
        params=(
            Param("serve", "boolean", "start the foreground review server (loopback only)"),
            Param("plane", "list", "plane root to serve (repeatable; default: this workspace)", flag="plane"),
            Param("host", "string", "bind host (loopback only)"),
            Param("port", "int", "bind port (0 chooses a free port)"),
            Param("no_browser", "boolean", "serve: do not open a browser automatically"),
            Param("url_file", "string", "serve: write the URL to this file (mode 0600)"),
            Param("poll", "boolean", "print open comments and the newest decision"),
            Param("ack", "boolean", "acknowledge one comment (requires --comment-id)"),
            Param("state", "boolean", "print one review snapshot"),
            Param("diff", "boolean", "print one file diff"),
            Param("comment", "boolean", "record a comment"),
            Param("reply", "boolean", "record a reply row (requires --comment-id and --body)"),
            Param("addressed", "boolean", "mark a root comment addressed (requires --comment-id)"),
            Param("resolve", "boolean", "route one comment to a node (requires --comment-id)"),
            Param("decision", "string", "record a decision", choices=("approve", "request_changes", "override")),
            Param("all", "boolean", "with --decision approve: approve the whole campaign commit set"),
            Param("report", "boolean", "write the deterministic campaign report"),
            Param("narrative", "string", "report: what-changed/risks text"),
            Param("design", "string", "report: design document reference"),
            Param("comment_id", "int", "comment id for --ack/--reply/--addressed/--resolve"),
            Param("parent_comment_id", "int", "with --comment: create a reply under this comment"),
            Param("addressing_commit", "string", "commit that answers the comment"),
            Param("target", "string", "deliver: target branch override"),
            Param("commit", "string", "commit to review or approve"),
            Param("file", "string", "file path"),
            Param("side", "string", "comment side", choices=("old", "new")),
            Param("line", "int", "line number"),
            Param("line_end", "int", "end line for a range"),
            Param("body", "string", "comment body"),
            Param("note", "string", "decision note"),
            Param("actor", "string", "who recorded the decision"),
            Param("campaign", "string", CAMPAIGN_HELP),
        ),
    ),
    Action(
        name="attempt",
        summary="record a subagent attempt's begin/end and metrics",
        params=(
            Param("begin", "boolean", "start an attempt"),
            Param("end", "boolean", "finish the running attempt for --node"),
            Param("node", "string", "node id"),
            Param("unit", "string", "unit name"),
            Param("agent", "string", "worker | planner | verifier"),
            Param("attempt", "int", "attempt number (default 1)"),
            Param("status", "string", "end: status, e.g. ok | failed | cancelled"),
            Param("exit_code", "int", "end: process exit code"),
            Param("turns", "int", "end: turn count"),
            Param("tool_calls", "int", "end: tool call count"),
            Param("tools", "string", "end: JSON tool histogram"),
            Param("tool_seconds", "string", "end: total tool seconds"),
            Param("tool_durations", "string", "end: JSON tool duration map"),
            Param("slowest_commands", "string", "end: JSON slowest command list"),
            Param("tokens_in", "int", "end: input tokens"),
            Param("tokens_out", "int", "end: output tokens"),
            Param("cost", "string", "end: approximate cost"),
            Param("last_tool", "string", "end: last tool name"),
            Param("error", "string", "end: error text"),
            Param("campaign", "string", CAMPAIGN_HELP),
        ),
    ),
    Action(
        name="progress",
        summary="time and tool breakdown for a campaign",
        params=(
            Param("node", "string", "show one node instead of the campaign"),
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
        base=params.get("base"),
        kind=params.get("kind") or "worker",
        main_branch=params.get("main_branch"),
        target_branch=params.get("target"),
        target_mode=params.get("target_mode"),
        worktree_branch=params.get("worktree_branch"),
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
    return service.status()


def _dispatch_deliver(service: "Service", p: dict[str, Any]) -> Any:
    return service.deliver(
        target=p.get("target"),
        source=p.get("source"),
        cleanup=p.get("cleanup") or "none",
        run_checks_flag=not p.get("no_checks"),
    )


def _dispatch_attempt(service: "Service", p: dict[str, Any]) -> Any:
    node = p.get("node")
    if not node:
        raise SlicemeError("attempt requires --node")
    attempt_number = int(p["attempt"]) if p.get("attempt") is not None else None
    if p.get("begin"):
        return service.begin_attempt(
            node=str(node),
            unit=p.get("unit"),
            attempt=attempt_number or 1,
            agent=p.get("agent") or "worker",
        )
    if p.get("end"):
        fields: dict[str, Any] = {}
        for key in (
            "status",
            "exit_code",
            "turns",
            "tool_calls",
            "tools",
            "tool_seconds",
            "tool_durations",
            "slowest_commands",
            "tokens_in",
            "tokens_out",
            "cost",
            "last_tool",
            "error",
        ):
            value = p.get(key)
            if value is None:
                continue
            if key in {"exit_code", "turns", "tool_calls", "tokens_in", "tokens_out"}:
                value = int(value)
            elif key in {"cost", "tool_seconds"}:
                value = float(value)
            elif key in {"tool_durations", "slowest_commands"}:
                try:
                    value = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise SlicemeError(
                        f"attempt --end {key} is not valid JSON: {exc}"
                    ) from None
            fields[key] = value
        result = service.end_attempt(
            node=str(node), attempt=attempt_number, **fields
        )
        if result is None:
            raise SlicemeError(f"no running attempt for node '{node}'")
        return result
    return service.attempts(node=str(node))


def _dispatch_progress(service: "Service", p: dict[str, Any]) -> Any:
    return service.progress(node=p.get("node"))


def _dispatch_wave(service: "Service", p: dict[str, Any]) -> Any:
    if p.get("open"):
        return {"unit": service.create_campaign_workspace()}
    if p.get("record"):
        if p.get("wave") is None:
            raise SlicemeError("wave --record requires --wave")
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
        # Serialize git mutation with the single executor's check runs.
        with service.executor().lock():
            return service.record_wave(
                int(p["wave"]),
                only=only,
                messages=messages,
                summary=p.get("summary"),
            )
    raise SlicemeError("wave needs --open or --record")


def _dispatch_exec(service: "Service", p: dict[str, Any]) -> Any:
    executor = service.executor()
    if p.get("validate"):
        info = service.sandbox_info(gpu_required=bool(p.get("gpu_required")))
        if not info.get("ok"):
            raise SlicemeError(str(info.get("error") or "sandbox gate failed"))
        return info
    if p.get("cancel"):
        if not p.get("job"):
            raise SlicemeError("exec --cancel requires --job")
        return {"job": executor.cancel(p["job"])}
    if p.get("wait"):
        if not p.get("job"):
            raise SlicemeError("exec --wait requires --job")
        return {"job": executor.wait(p["job"], timeout=float(p.get("timeout") or 600))}
    if p.get("run"):
        jobs = executor.drain(limit=p.get("limit") or None)
        return {"executed": len(jobs), "jobs": jobs}
    if p.get("submit"):
        result = executor.submit(
            source=p.get("source"),
            commit=p.get("commit"),
            commits=list(p.get("commits") or []),
            commands=list(p.get("command") or []),
            only=list(p.get("only") or []),
            sandbox=p.get("sandbox"),
            gpu=p.get("gpu") or "none",
            wave=p.get("wave"),
            requester=p.get("requester"),
            priority=int(p.get("priority") or 0),
            timeout=int(p.get("timeout") or 3600),
        )
        return {"cached": result["cached"], "jobs": result["jobs"], "job": result["job"]}
    return executor.status()


def _dispatch_review(service: "Service", p: dict[str, Any]) -> Any:
    from .review import api as review_api

    if p.get("serve"):
        roots = [Path(item) for item in (p.get("plane") or [])] or [service.root]
        return review_api.serve(
            roots,
            host=p.get("host") or "127.0.0.1",
            port=int(p.get("port") or 0),
            browser=not p.get("no_browser"),
            url_file=p.get("url_file"),
            campaign=p.get("campaign"),
        )
    if p.get("poll"):
        return review_api.poll(service, p)
    if p.get("ack"):
        return review_api.ack(service, p)
    if p.get("reply"):
        return review_api.reply(service, p)
    if p.get("addressed"):
        return review_api.addressed(service, p)
    if p.get("resolve"):
        return review_api.resolve(service, p)
    if p.get("comment"):
        return review_api.comment(service, p)
    if p.get("decision"):
        return review_api.decision(service, p)
    if p.get("report"):
        return review_api.report(service, p)
    if p.get("diff"):
        return review_api.diff(service, p)
    return review_api.state(service, p)


_HANDLERS = {
    "status": _dispatch_status,
    "deliver": _dispatch_deliver,
    "attempt": _dispatch_attempt,
    "progress": _dispatch_progress,
    "exec": _dispatch_exec,
    "wave": _dispatch_wave,
    "review": _dispatch_review,
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
