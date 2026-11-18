"""Notifications: who hears about what, in the app and by email.

    Event                               Who                                      Where
    A draft was sent for review         Managers and Admins (not the sender)     app + email
    A draft was approved or rejected    its uploader (not the reviewer)          app + email
    A post went live / failed / unknown the post's owner; failures: Admins too   app + email
    An invitation was created           the invited address                      email only

Each notification is written in the same transaction as its event (an outbox), so none is
lost in a crash or sent for a change that rolled back. The worker sends the email copies
(`EmailOutbox`), retrying with backoff; email is on only when SMTP_HOST is set, and each person
can turn their copies off. In-app notifications are always kept.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.api.database import (
    ApiJob,
    Notification,
    Publication,
    PublishingConnection,
    ScheduledPost,
    User,
    Workspace,
    WorkspaceMember,
)
from app.api.dependencies import CurrentMember, DbSession
from app.errors import ConfigError

log = logging.getLogger("notifications")

REVIEWER_ROLES = ("manager", "admin", "super_admin")
ADMIN_ROLES = ("admin", "super_admin")
MAX_EMAIL_ATTEMPTS = 6
EMAIL_LEASE = timedelta(minutes=5)


def mail_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST", "").strip())


def _base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Staging (inside the event's transaction) ─────────────────────────────────────────────


def stage(db: Session, *, workspace_id: str, recipients: Iterable[User], kind: str, title: str,
          body: str = "", job_id: str | None = None, route: str | None = None) -> int:
    email = mail_configured()
    count = 0
    for user in {u.id: u for u in recipients}.values():
        db.add(Notification(
            workspace_id=workspace_id, user_id=user.id, email=user.email, kind=kind,
            title=title[:200], body=body, job_id=job_id, route=route,
            email_status="pending" if email and user.email_notifications else "off",
            email_next_at=_now() if email and user.email_notifications else None,
        ))
        count += 1
    return count


def _members(db: Session, workspace_id: str, roles: tuple[str, ...],
             exclude: str | None = None) -> list[User]:
    return list(db.scalars(
        select(User).join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.role.in_(roles),
               User.id != (exclude or ""))
    ))


def _label(job: ApiJob) -> str:
    return job.filename or "a draft"


def draft_submitted(db: Session, job: ApiJob, actor) -> None:
    stage(db, workspace_id=job.workspace_id,
          recipients=_members(db, job.workspace_id, REVIEWER_ROLES, exclude=actor.id),
          kind="draft.submitted", title=f"New draft to review: {_label(job)}",
          body=f"{actor.email} sent {_label(job)} for review. Check every name, price, date and "
               "place before approving it.",
          job_id=job.id, route="review")


def draft_decided(db: Session, job: ApiJob, actor, decision: str, comment: str | None) -> None:
    owner = db.get(User, job.owner_id)
    if owner is None or owner.id == actor.id:
        return
    if decision == "approved":
        title, body = (f"Approved: {_label(job)}",
                       f"{actor.email} approved your draft. You can put it on the calendar now.")
    else:
        title = f"Changes needed: {_label(job)}"
        body = f"{actor.email} asked for changes" + (f": \"{comment}\"" if comment else ".") + \
            " Edit the draft, then send it for review again."
    stage(db, workspace_id=job.workspace_id, recipients=[owner], kind=f"job.{decision}",
          title=title, body=body, job_id=job.id, route="library")


def publication_outcome(db: Session, pub: Publication) -> None:
    post = db.get(ScheduledPost, pub.scheduled_post_id)
    conn = db.get(PublishingConnection, pub.connection_id)
    job = db.get(ApiJob, post.job_id) if post else None
    owner = db.get(User, post.owner_id) if post else None
    if post is None or job is None:
        return
    where = conn.account_name if conn else "the account"
    if pub.status == "published":
        recipients = [owner] if owner else []
        title = f"Live on {where}: {_label(job)}"
        body = "Your post was published." + (f" {pub.external_url}" if pub.external_url else "")
    else:
        recipients = ([owner] if owner else []) + _members(db, pub.workspace_id, ADMIN_ROLES)
        if pub.status == "unknown":
            title = f"Check {where}: {_label(job)}"
            body = ("It isn't known whether this post went out. Look at the account first: if "
                    "the post isn't there, select \"I checked: send again\"; if it is, leave it.")
        else:
            title = f"Didn't publish to {where}: {_label(job)}"
            body = (pub.last_error or "The platform refused the post.") + \
                " Open the post on the calendar to retry, or post it by hand."
    stage(db, workspace_id=pub.workspace_id, recipients=recipients,
          kind=f"publication.{pub.status}", title=title, body=body, job_id=job.id,
          route="calendar")


def invitation(db: Session, *, workspace_id: str, email: str, token: str, role: str,
               inviter_email: str, cipher) -> bool:
    """Email the invitation link. Returns False when email is off (the inviter shares it)."""
    if not mail_configured() or not _base_url():
        return False
    workspace = db.get(Workspace, workspace_id)
    name = workspace.name if workspace else "a MediaFlow workspace"
    db.add(Notification(
        workspace_id=workspace_id, user_id=None, email=email, kind="invitation",
        title=f"You're invited to {name} on MediaFlow",
        body=f"{inviter_email} invited you to join {name} as {role}. The link works once and "
             "expires; if it has, ask for a new one.",
        secret=cipher.seal(f"{_base_url()}/#/invite/{token}"),
        email_status="pending", email_next_at=_now(),
    ))
    return True


# ── Sending ──────────────────────────────────────────────────────────────────────────────


class PermanentEmailError(Exception):
    """The server refused this message or address; retrying won't help."""


class Mailer:
    def __init__(self, *, host: str, port: int, sender: str, username: str = "",
                 password: str = "", security: str = "starttls", timeout_s: float = 20.0):
        self.host, self.port, self.sender = host, port, sender
        self.username, self.password = username, password
        self.security, self.timeout_s = security, timeout_s

    @classmethod
    def from_env(cls) -> Mailer | None:
        host = os.environ.get("SMTP_HOST", "").strip()
        if not host:
            return None
        security = os.environ.get("SMTP_SECURITY", "starttls").strip().lower() or "starttls"
        if security not in ("starttls", "ssl", "none"):
            raise ConfigError("SMTP_SECURITY must be starttls, ssl or none.")
        sender = os.environ.get("SMTP_FROM", "").strip()
        if not sender:
            raise ConfigError("SMTP_HOST is set, so SMTP_FROM is required "
                              "(e.g. MediaFlow <noreply@example.com>).")
        default_port = {"starttls": 587, "ssl": 465, "none": 25}[security]
        try:
            port = int(os.environ.get("SMTP_PORT", "").strip() or default_port)
        except ValueError:
            raise ConfigError("SMTP_PORT must be a number.") from None
        return cls(host=host, port=port, sender=sender,
                   username=os.environ.get("SMTP_USERNAME", "").strip(),
                   password=os.environ.get("SMTP_PASSWORD", ""),
                   security=security)

    def send(self, to: str, subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg["Message-ID"] = make_msgid(domain=self.sender.rsplit("@", 1)[-1].strip("> "))
        msg.set_content(text)
        context = ssl.create_default_context()
        try:
            if self.security == "ssl":
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout_s,
                                          context=context)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=self.timeout_s)
            with server:
                if self.security == "starttls":
                    server.starttls(context=context)
                if self.username:
                    server.login(self.username, self.password)
                server.send_message(msg)
        except smtplib.SMTPRecipientsRefused as err:
            raise PermanentEmailError(f"The mail server refused {to}") from err
        except smtplib.SMTPResponseException as err:
            if 500 <= err.smtp_code < 600 and err.smtp_code not in (535, 554):
                raise PermanentEmailError(f"The mail server refused it ({err.smtp_code})") from err
            raise


def compose(n: Notification, link: str | None) -> str:
    lines = [n.title, "", n.body]
    route = f"{n.route}?job={n.job_id}" if n.route and n.job_id else n.route  # opens the draft
    url = link or (f"{_base_url()}/#/{route}" if _base_url() and route else None)
    if url:
        lines += ["", url]
    if n.user_id:
        lines += ["", "--", "You get these emails from MediaFlow. To stop them, open the bell "
                  "in MediaFlow and turn off \"Email me\"."]
    return "\n".join(lines)


class EmailOutbox:
    """Sends pending email copies. Safe with several workers: rows are leased under a lock."""

    def __init__(self, session_factory: Callable[[], Session], mailer: Mailer, *, cipher=None,
                 batch: int = 20, now: Callable[[], datetime] = _now):
        self.sessions, self.mailer, self.cipher = session_factory, mailer, cipher
        self.batch, self.now = batch, now

    def run_once(self) -> dict[str, int]:
        now = self.now()
        with self.sessions.begin() as db:
            rows = db.scalars(
                select(Notification)
                .where(Notification.email_status == "pending",
                       or_(Notification.email_next_at.is_(None), Notification.email_next_at <= now))
                .order_by(Notification.created_at).limit(self.batch)
                .with_for_update(skip_locked=True)
            ).all()
            leased = []
            for n in rows:
                n.email_attempts += 1
                n.email_next_at = now + EMAIL_LEASE  # if this worker dies, another retries
                leased.append((n.id, n.email, n.title, compose(n, self._link(n)),
                               n.email_attempts))
        counts = {"sent": 0, "retry": 0, "failed": 0}
        for nid, to, subject, text, attempt in leased:
            counts[self._send(nid, to, subject, text, attempt)] += 1
        return counts

    def _link(self, n: Notification) -> str | None:
        if not n.secret:
            return None
        if self.cipher is None:
            return None
        try:
            return self.cipher.open(n.secret)
        except Exception:
            log.warning("could not open a notification link", extra={"notification_id": n.id})
            return None

    def _send(self, nid: str, to: str | None, subject: str, text: str, attempt: int) -> str:
        error, outcome = None, "sent"
        try:
            if not to:
                raise PermanentEmailError("No address")
            self.mailer.send(to, subject, text)
        except PermanentEmailError as err:
            error, outcome = str(err), "failed"
        except Exception as err:  # connection, timeout, 4xx: try again later
            error = f"{type(err).__name__}: {err}"[:300]
            outcome = "failed" if attempt >= MAX_EMAIL_ATTEMPTS else "retry"
        with self.sessions.begin() as db:
            n = db.get(Notification, nid)
            if n is None:
                return outcome
            n.email_error = error
            if outcome == "sent":
                n.email_status, n.email_sent_at, n.email_next_at = "sent", self.now(), None
            elif outcome == "failed":
                n.email_status, n.email_next_at = "failed", None
            else:
                n.email_next_at = self.now() + timedelta(minutes=2 ** attempt)
            if outcome != "retry":
                n.secret = None  # an invitation link doesn't outlive its delivery
        if error:
            log.warning("notification email %s", outcome,
                        extra={"notification_id": nid, "error": error})
        return outcome


# ── API ──────────────────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1")


class NotificationView(BaseModel):
    id: str
    kind: str
    title: str
    body: str
    job_id: str | None
    route: str | None
    created_at: datetime
    read: bool


class NotificationList(BaseModel):
    items: list[NotificationView]
    unread: int


class MarkRead(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=200)
    all: bool = False


class Preferences(BaseModel):
    email: bool
    email_available: bool = False


class PreferencesUpdate(BaseModel):
    email: bool


def _mine(actor):
    return (Notification.user_id == actor.id, Notification.workspace_id == actor.workspace_id)


@router.get("/notifications", response_model=NotificationList)
def list_notifications(actor: CurrentMember, db: DbSession, limit: int = 30):
    limit = max(1, min(limit, 100))
    rows = db.scalars(select(Notification).where(*_mine(actor))
                      .order_by(Notification.created_at.desc()).limit(limit)).all()
    unread = len(db.scalars(select(Notification.id).where(
        *_mine(actor), Notification.read_at.is_(None)).limit(100)).all())
    return NotificationList(unread=unread, items=[NotificationView(
        id=n.id, kind=n.kind, title=n.title, body=n.body, job_id=n.job_id, route=n.route,
        created_at=n.created_at, read=n.read_at is not None) for n in rows])


@router.post("/notifications/read", status_code=status.HTTP_204_NO_CONTENT)
def mark_read(payload: MarkRead, actor: CurrentMember, db: DbSession):
    query = update(Notification).where(*_mine(actor), Notification.read_at.is_(None))
    if not payload.all:
        query = query.where(Notification.id.in_(payload.ids or [""]))
    db.execute(query.values(read_at=_now()).execution_options(synchronize_session=False))
    db.commit()


@router.get("/notifications/preferences", response_model=Preferences)
def get_preferences(actor: CurrentMember):
    return Preferences(email=actor.user.email_notifications, email_available=mail_configured())


@router.put("/notifications/preferences", response_model=Preferences)
def set_preferences(payload: PreferencesUpdate, actor: CurrentMember, db: DbSession):
    user = db.get(User, actor.id)
    user.email_notifications = payload.email
    if not payload.email:  # stop copies already waiting
        db.execute(update(Notification).where(
            Notification.user_id == actor.id, Notification.email_status == "pending")
            .values(email_status="off", email_next_at=None)
            .execution_options(synchronize_session=False))
    db.commit()
    return Preferences(email=payload.email, email_available=mail_configured())
