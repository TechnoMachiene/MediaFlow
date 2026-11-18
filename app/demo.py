"""Demo mode: the whole product without Gemini credentials.

DemoClient stands in for Gemini when DEMO_MODE=1. It looks at the real upload (dimensions,
orientation, dominant colour for images; size for videos) and writes a deterministic draft in
exactly the JSON shape the system prompt asks Gemini for, so every screen behaves as it would
in production. Results are labelled with model "demo" and the UI shows a demo banner.

`python -m app.demo seed` creates a demo workspace with three accounts (admin, manager,
member) and refuses to run unless DEMO_MODE=1, so demo credentials never reach a real install.

Containment - the scripted provider is for local/demo use only, enforced in code:
- It is only ever built when DEMO_MODE=1 (app/gemini_client.py: build_client).
- DEMO_MODE=1 refuses to start with a GEMINI_API_KEY set, so a demo can't be mistaken for (or
  slipped into) a real deployment that has credentials.
- DEMO_MODE=1 requires a Redis key prefix starting with "demo", so a demo worker can never
  claim jobs from, or write fake drafts into, a real queue (default prefix "ma").
- Demo accounts are only seeded into an empty database or one this demo seeded before, so
  known credentials can never be added to a database that holds real users.
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import os
import sys
import time

from PIL import Image, ImageOps, UnidentifiedImageError

from app.errors import ConfigError, Permanent
from app.jobs import JobSpec

DEMO_MODEL = "demo"
DEMO_QUEUE_PREFIX = "demo"
DEMO_PASSWORD = "demo-password-123"
DEMO_WORKSPACE = "Demo Studio"
DEMO_ACCOUNTS = (
    ("demo-admin@example.com", "admin"),
    ("demo-manager@example.com", "manager"),
    ("demo-member@example.com", "user"),
)

class DemoIsolationError(ConfigError):
    """Demo mode was pointed at something real."""


def check_demo_isolation(*, api_key: str, prefix: str) -> None:
    if api_key:
        raise DemoIsolationError(
            "DEMO_MODE=1 refuses to run with GEMINI_API_KEY set: demo mode is for trying the app "
            "without Gemini. Unset GEMINI_API_KEY for the demo, or unset DEMO_MODE for real use."
        )
    if not prefix.startswith(DEMO_QUEUE_PREFIX):
        raise DemoIsolationError(
            f"DEMO_MODE=1 needs QUEUE_PREFIX to start with '{DEMO_QUEUE_PREFIX}' (got {prefix!r}) "
            "so demo workers can never touch a real job queue."
        )


_HUES = (
    (15, "red", "bold, high-energy"), (40, "orange", "warm, upbeat"),
    (65, "golden", "sunny, optimistic"), (160, "green", "fresh, natural"),
    (200, "teal", "calm, clean"), (250, "blue", "trustworthy, cool"),
    (290, "purple", "creative, playful"), (335, "pink", "friendly, vibrant"),
    (361, "red", "bold, high-energy"),
)


def _describe_colour(rgb: tuple[int, int, int]) -> tuple[str, str]:
    r, g, b = (c / 255 for c in rgb)
    h, lightness, s = colorsys.rgb_to_hls(r, g, b)
    if s < 0.15 or lightness < 0.08 or lightness > 0.92:
        if lightness < 0.25:
            return "dark", "moody, premium"
        if lightness > 0.75:
            return "bright white", "minimal, airy"
        return "neutral grey", "understated, modern"
    degrees = h * 360
    for limit, name, tone in _HUES:
        if degrees < limit:
            return name, tone
    return "red", "bold, high-energy"  # pragma: no cover - 361 catches everything


def _image_facts(path: str) -> dict:
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            width, height = im.size
            colour = im.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
    except (UnidentifiedImageError, OSError) as e:
        raise Permanent(f"demo client could not read image: {e}") from None
    ratio = width / height if height else 1
    shape = "square" if 0.9 <= ratio <= 1.1 else ("portrait" if ratio < 1 else "landscape")
    name, tone = _describe_colour(colour)
    return {"width": width, "height": height, "shape": shape, "colour": name, "tone": tone}


class DemoClient:
    """Offline, deterministic AnalysisClient. Same bytes -> same draft."""

    model = "demo"

    def __init__(self, delay_s: float = 1.5):
        self.delay_s = delay_s  # long enough to watch a job move through the queue

    def analyse(self, spec: JobSpec) -> dict:
        if self.delay_s:
            time.sleep(self.delay_s)
        if not os.path.isfile(spec.path):
            raise Permanent(f"file missing: {spec.path}")
        variant = int(hashlib.sha256(spec.sha256.encode()).hexdigest(), 16)
        if spec.kind == "image":
            draft = self._image_draft(_image_facts(spec.path), variant)
        else:
            draft = self._video_draft(os.path.getsize(spec.path), variant)
        return {
            "text": json.dumps(draft, ensure_ascii=False, indent=2),
            "kind": spec.kind,
            "model": self.model,
            "tokens": 0,
        }

    @staticmethod
    def _pick(options: tuple[str, ...], variant: int, salt: int = 0) -> str:
        return options[(variant >> salt) % len(options)]

    # The same shape the production prompt's reply is converted to (app/copy_format.py):
    # Facebook and Instagram copy plus a summary. Generic on purpose: demo drafts never
    # state facts about a real company or project.
    def _image_draft(self, f: dict, v: int) -> dict:
        hook = self._pick(
            ("Stop scrolling for a second.", "This one deserves a closer look.",
             "Saved the best shot for today."), v)
        colour = f["colour"].capitalize()
        return {
            "facebook": {
                "caption": f"{hook} {colour} tones and a {f['tone']} mood in this "
                           f"{f['shape']} shot.\n\nTell us what you see first. "
                           "(Demo draft: connect Gemini for real copy.)",
            },
            "instagram": {
                "caption": f"{hook} {colour} tones, {f['tone']} mood. What would you title "
                           "this? (Demo draft: connect Gemini for real copy.)",
                "hashtags": ["#Demo", f"#{f['colour'].title().replace(' ', '')}", "#MediaFlow"],
            },
            "content_summary": {
                "post_type": "Demo post",
                "alt_text": [f"A {f['shape']} image with a {f['colour']} palette, "
                             f"{f['width']} by {f['height']} pixels."],
                "first_comment": "Which colour should we feature next?",
                "notes": ["Demo mode: written offline from the image's colours and shape, "
                          "not by Gemini."],
            },
        }

    def _video_draft(self, size: int, v: int) -> dict:
        length = "short clip" if size < 20 * 1024 * 1024 else "longer video"
        hook = self._pick(
            ("Wait for the last second.", "Here's what happened next.",
             "Watch this before you scroll."), v)
        return {
            "facebook": {
                "caption": f"{hook} Sharing a {length} from this week. "
                           "(Demo draft: connect Gemini for real copy.)",
            },
            "instagram": {
                "caption": f"{hook} A {length} worth the replay. "
                           "(Demo draft: connect Gemini for real copy.)",
                "hashtags": ["#Demo", "#Video", "#MediaFlow"],
                "reel_on_screen_text": [hook, "Keep watching"],
                "cover_title": "Worth the replay",
            },
            "content_summary": {
                "post_type": "Demo post",
                "alt_text": [f"A {length}."],
                "first_comment": "Would you want a part two?",
                "notes": ["Demo mode: written offline from the file's size, not by Gemini."],
            },
        }


def seed(session_factory) -> list[tuple[str, str]]:
    """Create the demo workspace and any missing demo accounts. Returns [(email, role)]."""
    from sqlalchemy import select

    from app.api.database import User, Workspace, WorkspaceMember
    from app.api.security import hash_password

    with session_factory.begin() as db:
        workspace = db.scalar(select(Workspace).where(Workspace.name == DEMO_WORKSPACE))
        if workspace is None:
            # First run: only an empty database may become a demo database. (Later runs find
            # the demo workspace, so sign-ups made by demo visitors don't block restarts.)
            if db.scalar(select(User.id).limit(1)) is not None:
                raise DemoIsolationError(
                    "DEMO_MODE=1 is pointed at a database that already has real accounts; "
                    "refusing to add demo accounts with a published password. Use an empty "
                    "database for the demo."
                )
            workspace = Workspace(name=DEMO_WORKSPACE)
            db.add(workspace)
            db.flush()
        password_hash = None
        for email, role in DEMO_ACCOUNTS:
            user = db.scalar(select(User).where(User.email == email))
            if user is None:
                password_hash = password_hash or hash_password(DEMO_PASSWORD)
                user = User(email=email, password_hash=password_hash, role="user")
                db.add(user)
                db.flush()
            # Existing demo accounts are left alone so a restart doesn't sign everyone out.
            if db.get(WorkspaceMember, (workspace.id, user.id)) is None:
                db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=role))
    return list(DEMO_ACCOUNTS)


def demo_banner() -> str:
    width = 64
    rows = [f"  {role.replace('user', 'member'):<8}  {email}" for email, role in DEMO_ACCOUNTS]
    return "\n".join([
        "",
        "=" * width,
        "  MediaFlow DEMO MODE - drafts are simulated, no Gemini calls",
        "  Sign in with any of these (password for all: " + DEMO_PASSWORD + "):",
        *rows,
        "=" * width,
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    from app.api.config import ApiSettings
    from app.api.database import Base, create_database
    from app.errors import ConfigError

    load_dotenv()
    parser = argparse.ArgumentParser(prog="app.demo")
    parser.add_argument("command", choices=["seed"])
    parser.parse_args(argv)
    try:
        cfg = ApiSettings.from_env()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if not cfg.demo_mode:
        print("refusing to seed demo accounts: set DEMO_MODE=1 first", file=sys.stderr)
        return 2
    engine, sessions = create_database(cfg.database_url)
    try:
        if cfg.database_url.startswith("sqlite"):
            Base.metadata.create_all(engine)  # demo SQLite has no migration history
        accounts = seed(sessions)
    finally:
        engine.dispose()
    print(f"Demo workspace '{DEMO_WORKSPACE}' is ready. Password for every account: "
          f"{DEMO_PASSWORD}")
    for email, role in accounts:
        print(f"  {role:<8} {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
