# Authorization audit trail

Every change to **who can do what**, and every decision made with that authority, is recorded
in `audit_events`: who did it, to what, the value before and after, and the request it came
from. Admins read it per workspace (`GET /api/v1/audit-events`, or **Audit** in the web app).

## Guarantees

| Guarantee | How | Test |
|---|---|---|
| **No change without its record, and no record without its change** | `audit.record()` adds the event to the same database transaction as the change. Either both commit or neither does | `test_a_change_and_its_audit_event_commit_together` |
| **Only real changes are recorded** | Refused requests (403/404/409) roll back before anything is written. No-ops (setting a role it already has, cancelling twice) record nothing | `test_refused_and_no_op_requests_leave_no_trace` |
| **History can't be rewritten** | On PostgreSQL, a trigger rejects every `UPDATE` and `DELETE` on `audit_events`, so even the application's own credentials can only append | `tests/integration/test_postgres.py` |
| **History outlives the people in it** | Actor and target are snapshots (id and email as they were), with no foreign keys to `users`. Deleting an account keeps every event about it | `test_history_survives_deleting_the_people_in_it` |
| **Out-of-band changes are visible** | The admin CLI (`app.api.admin`) records its changes with the actor `system:cli` | `test_admin_cli_changes_are_audited_as_the_system` |
| **Tenants see only their own trail** | Events belong to one workspace; only its admins can read them | `test_only_admins_read_the_trail_and_only_their_workspaces` |
| **Events join logs and traces** | `request_id` is the request's `X-Request-ID`, which is on every log line and the trace for that request | `test_role_change_records_who_whom_before_and_after` |

## What is recorded

`tests/unit/test_audit_catalogue.py` checks that this table matches `ACTIONS` in
`app/api/audit.py`.

<!-- actions:start -->
| Action | Recorded when | `before` → `after` |
|---|---|---|
| `member.created` | An account was created directly in the workspace | → role |
| `member.role_changed` | A member's role in the workspace changed | role → role |
| `member.email_changed` | A member's sign-in email was changed by an admin | email → email |
| `member.password_reset` | A member's password was reset by an admin (signs them out) | (no values; never the password) |
| `member.removed` | A member was removed, with their jobs in this workspace | role → jobs deleted |
| `member.joined` | Someone joined by accepting an invitation | → role, invitation, new account? |
| `member.left` | A member left the workspace | role → |
| `invitation.created` | An email was invited with a role (replacing any earlier link) | → role, expiry, links replaced |
| `invitation.revoked` | A pending invitation was withdrawn | role → |
| `job.approved` | A reviewer approved a draft | pending → approved, owner |
| `job.rejected` | A reviewer rejected a draft | pending → rejected, owner |
| `job.cancelled` | A queued job was cancelled | status → cancelled |
| `draft.edited` | A draft was edited or an older version restored (sends it back for review) | version, approval → version, approval, restored from |
| `post.scheduled` | An approved draft was put on the calendar | → time, timezone, job |
| `post.rescheduled` | A calendar entry's time or timezone changed | time, timezone → time, timezone |
| `post.cancelled` | A calendar entry was cancelled | time, timezone → cancelled |
| `connection.connected` | A publishing account was connected (or reconnected) | → provider, channel, account |
| `connection.disconnected` | A publishing account was disconnected; its credentials wiped | status → disconnected |
| `publication.requested` | A scheduled post was set to publish to an account | → account, time |
| `publication.retried` | A failed or uncertain publication was sent again | status, error → queued |
| `publication.cancelled` | A pending publication was cancelled | status → cancelled |
| `workspace.created` | The workspace was created | → name, first admin |
| `workspace.renamed` | The workspace was renamed | name → name |
<!-- actions:end -->

Each event also has `actor_id`, `actor_email`, `target_type` (`user`, `invitation`, `job`,
`post`, `workspace`), `target_id`, `target_label` (an email or filename, for reading), and
`created_at`.

Deliberately **not** recorded: uploads, sign-ins and reads. They are high-volume, and they are
already in the access log with `user_id`, `workspace_id` and `request_id` (see
[OPERATIONS.md](OPERATIONS.md)). Refused attempts are logged as `4xx` access-log lines rather
than audit events, so the trail stays a record of what actually changed.

## Reading it

`GET /api/v1/audit-events` (admins of the current workspace), newest first:

| Parameter | Meaning |
|---|---|
| `category` | `member`, `invitation`, `job`, `draft`, `post`, `connection`, `publication` or `workspace` |
| `action` | One exact action from the table above (`422` if unknown) |
| `actor_id` | Everything one person did |
| `target_id` | One user's, job's or post's whole history |
| `before`, `limit` | Keyset paging: pass the response's `next_before` to get older events. `limit` is 1–200 (default 50) |

Ids are monotonic, so pages never shift while new events arrive.

## Retention

Events are kept indefinitely. Removing them is a deliberate database operation (drop the
trigger, delete, restore the trigger) that belongs in a change-controlled maintenance window,
not in the application.
