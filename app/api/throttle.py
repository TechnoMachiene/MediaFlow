"""Throttling for sign-in and signup, shared by every API process through Redis.

This is defence in depth: the reverse proxy should rate-limit these routes at the edge too
(docs/DEPLOYMENT.md), but the API must not depend on the proxy being configured correctly.

* **Sign-in:** failed attempts are counted per account and per client IP in a sliding-ish
  fixed window (LOGIN_WINDOW_S, default 15 min). Past LOGIN_MAX_FAILURES (10) for an account,
  or LOGIN_IP_MAX_FAILURES (50, room for an office behind one NAT) for an IP, sign-in answers
  429 with Retry-After until the window ends, even with the right password, so a blocked
  attempt can't be used to test passwords. A successful sign-in clears that account's count.
* **Signup:** every attempt counts, per IP (SIGNUP_IP_MAX, 10 per hour).

Keys hold SHA-256 digests, never raw emails or addresses. If Redis is unreachable the throttle
fails open (and logs it): sign-in depends on PostgreSQL, and a Redis outage must not lock
everyone out.
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import HTTPException, Request, status

log = logging.getLogger("api.throttle")


def _digest(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()[:32]


def client_ip(request: Request) -> str:
    # With --proxy-headers and FORWARDED_ALLOW_IPS set to the proxy, uvicorn has already put
    # the real client address here (docs/DEPLOYMENT.md).
    return request.client.host if request.client else "unknown"


class Throttle:
    def __init__(self, r, prefix: str, *, max_failures: int = 10, ip_max_failures: int = 50,
                 window_s: int = 900, signup_ip_max: int = 10, signup_window_s: int = 3600):
        self.r, self.p = r, f"{prefix}:throttle"
        self.max_failures, self.ip_max_failures = max_failures, ip_max_failures
        self.window_s = window_s
        self.signup_ip_max, self.signup_window_s = signup_ip_max, signup_window_s

    def _keys(self, ip: str, email: str) -> tuple[str, str]:
        return f"{self.p}:login:acct:{_digest(email)}", f"{self.p}:login:ip:{_digest(ip)}"

    def _refuse(self, key: str) -> None:
        wait = max(1, self.r.ttl(key))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Try again later.",
            headers={"Retry-After": str(wait)},
        )

    def check_login(self, ip: str, email: str) -> None:
        if self.r is None:
            return
        account, address = self._keys(ip, email)
        try:
            counts = self.r.mget(account, address)
        except Exception:
            log.warning("sign-in throttle unavailable; allowing the attempt", exc_info=True)
            return
        if int(counts[0] or 0) >= self.max_failures:
            self._refuse(account)
        if int(counts[1] or 0) >= self.ip_max_failures:
            self._refuse(address)

    def login_failed(self, ip: str, email: str) -> None:
        if self.r is None:
            return
        try:
            pipe = self.r.pipeline()
            for key in self._keys(ip, email):
                pipe.incr(key)
                pipe.expire(key, self.window_s, nx=True)
            pipe.execute()
        except Exception:
            log.warning("could not record a failed sign-in", exc_info=True)

    def login_succeeded(self, ip: str, email: str) -> None:
        if self.r is None:
            return
        try:
            self.r.delete(self._keys(ip, email)[0])
        except Exception:
            log.warning("could not clear the sign-in throttle", exc_info=True)

    def check_signup(self, ip: str) -> None:
        if self.r is None:
            return
        key = f"{self.p}:signup:ip:{_digest(ip)}"
        try:
            pipe = self.r.pipeline()
            pipe.incr(key)
            pipe.expire(key, self.signup_window_s, nx=True)
            count = pipe.execute()[0]
        except Exception:
            log.warning("signup throttle unavailable; allowing the attempt", exc_info=True)
            return
        if count > self.signup_ip_max:
            self._refuse(key)
