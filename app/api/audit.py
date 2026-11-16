"""Authorization audit trail: who changed what, in which workspace.

`record()` adds an `AuditEvent` to the caller's session and never commits, so the event and
the change it describes commit (or roll back) together. Only changes that actually happened
are recorded: a refused request (403/404/409) leaves no event, and a no-op (setting a role to
the role it already has) records nothing.

The catalogue below is the contract shown in docs/AUDIT.md; a test checks they match.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.api.database import AuditEvent
from app.obs import context as obs_context

# action -> what it means. Grouped by the prefix the API filter and the UI use.
ACTIONS: dict[str, str] = {
    "member.created": "An account was created directly in the workspace",
    "member.role_changed": "A member's role in the workspace changed",
    "member.email_changed": "A member's sign-in email was changed by an admin",
    "member.password_reset": "A member's password was reset by an admin (signs them out)",
    "member.removed": "A member was removed, with their jobs in this workspace",
    "member.joined": "Someone joined by accepting an invitation",
    "member.left": "A member left the workspace",
    "invitation.created": "An email was invited with a role (replacing any earlier link)",
    "invitation.revoked": "A pending invitation was withdrawn",
    "job.approved": "A reviewer approved a draft",
    "job.rejected": "A reviewer rejected a draft",
    "job.cancelled": "A queued job was cancelled",
    "post.scheduled": "An approved draft was put on the calendar",
    "post.rescheduled": "A calendar entry's time or timezone changed",
    "post.cancelled": "A calendar entry was cancelled",
    "workspace.created": "The workspace was created",
    "workspace.renamed": "The workspace was renamed",
}
CATEGORIES = ("member", "invitation", "job", "post", "workspace")

# Out-of-band changes (the admin CLI) have no signed-in actor.
SYSTEM_ACTOR = "system:cli"


def record(
    db: Session,
    *,
    workspace_id: str,
    actor: Any | None,
    action: str,
    target_type: str,
    target_id: str,
    target_label: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
) -> AuditEvent:
    """Stage an audit event in `db`'s transaction. `actor` is a User or Member (anything with
    `.id` and `.email`), or None for a system change."""
    if action not in ACTIONS:
        raise ValueError(f"unknown audit action {action!r}")
    event = AuditEvent(
        workspace_id=workspace_id,
        actor_id=getattr(actor, "id", None),
        actor_email=getattr(actor, "email", None) or SYSTEM_ACTOR,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        target_label=target_label,
        before=before,
        after=after,
        request_id=obs_context.get("request_id") or None,
    )
    db.add(event)
    return event
