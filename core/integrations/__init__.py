"""
Optional integrations Nova can talk to without absorbing their codebases.

Each module in this package is a passive bridge to an external tool:
  * silentguard — read-only file-based feed of network observations.
  * nexanote   — HTTP API for notes (read by default; writes opt-in).
  * github     — read-only HTTP bridge to the GitHub REST API for
                 admin-only issue / PR visibility (no writes in v1).

Every integration is gated behind a per-user or host-level switch and
is safe to import even when the underlying tool is missing or
unreachable. Public helpers must never raise on the happy "tool
absent" path; they return empty results or a `{"state": "..."}`
status dict instead.
"""
