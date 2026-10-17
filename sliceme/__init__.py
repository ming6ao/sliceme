"""Sliceme — local coordination for parallel coding agents.

This package implements the campaign engine described in ``docs/guide.md`` and
``docs/reference.md``: worktree-per-unit isolation, plan-time directory
ownership, verification pinned to content fingerprints, DAG phase planning, and
agent-callable integration onto a campaign feature branch.

The service layer (``sliceme.service``) is the single owner of state and is
called by every adapter (CLI, pi): one engine, many adapters, no adapter owns
state.  This tree is a dependency-free Python implementation so the behaviour
can be exercised end to end.
"""

__version__ = "0.3.0"
