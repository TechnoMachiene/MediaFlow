"""The allowed status changes of a durable job row (`api_jobs.status`).

The table is the contract described in docs/JOB_LIFECYCLE.md ("Allowed transitions"); a test
checks that the two never drift apart. Every writer of `api_jobs.status` goes through it:
the worker hooks (as a SQL predicate, so a late or replayed write loses the race safely), the
API read/cancel paths, and the reconciler (which only ever touches active rows).

The rule, in words:

* An **active** job may move to any status. Worker status hooks are best effort, so the row can
  legitimately skip states (``queued`` straight to ``dead``) or step back to ``queued`` when a
  lease expires or the reconciler re-queues a lost job.
* ``done`` and ``cancelled`` are **final**. Nothing moves a job out of them.
* ``failed`` and ``dead`` accept only ``done``: under at-least-once delivery a second attempt
  can finish after the first gave up, and a real result is always worth keeping. They never go
  back to an active status, so a stale ``processing``/``retrying`` hook can't revive them.
"""

from __future__ import annotations

from app.api.database import ACTIVE_STATUSES

FINAL_STATUSES = frozenset({"done", "cancelled"})
FAILED_STATUSES = frozenset({"failed", "dead"})
ALL_STATUSES = ACTIVE_STATUSES | FINAL_STATUSES | FAILED_STATUSES

TRANSITIONS: dict[str, frozenset[str]] = {
    **{state: ALL_STATUSES - {state} for state in ACTIVE_STATUSES},
    **{state: frozenset() for state in FINAL_STATUSES},
    **{state: frozenset({"done"}) for state in FAILED_STATUSES},
}


def can_transition(old: str, new: str) -> bool:
    """True if a row in `old` may be written as `new`. Writing the same status is a no-op."""
    return old == new or new in TRANSITIONS.get(old, frozenset())


def allowed_from(new: str) -> frozenset[str]:
    """The statuses a row may be in for a write of `new` to apply (for SQL `WHERE ... IN`).
    Includes `new` itself, so a repeated write (another `retrying` with a newer error) lands."""
    return frozenset(old for old in TRANSITIONS if can_transition(old, new))


def set_status(row, new: str) -> bool:
    """Move an ORM row to `new` if the table allows it. Returns whether it applied."""
    if not can_transition(row.status, new):
        return False
    row.status = new
    return True
