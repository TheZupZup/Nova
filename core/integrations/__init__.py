"""
Optional integrations Nova can talk to without absorbing their codebases.

Each module in this package is a passive bridge to an external tool:
  * silentguard — read-only file-based feed of network observations.
  * nexanote   — HTTP API for notes (read by default; writes opt-in).

Every integration is gated behind a per-user setting and is safe to
import even when the underlying tool is missing or unreachable. Public
helpers must never raise on the happy "tool absent" path; they return
empty results or a `{"state": "..."}` status dict instead.
"""
