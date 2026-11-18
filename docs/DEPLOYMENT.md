# Deploying MediaFlow on a public host

The Compose stack (`compose.yaml`) is safe on a private network as it is. Before anyone on the
internet can reach it, put it behind HTTPS, set strong secrets, and have a backup you have
restored at least once. This page is that checklist; each item is enforced or tested where
possible, and says so.

## 1. Secrets

| Setting | Requirement | Enforced |
|---|---|---|
| `POSTGRES_PASSWORD` | 16+ random characters: `openssl rand -hex 24` | Compose refuses to start without it; the API and worker refuse a weak or well-known database password whenever the database is not on localhost (`check_database_credentials`, tested in `tests/unit/test_api_config.py`) |
| `JWT_SECRET` | 32+ random bytes: `openssl rand -hex 32` | Refused below 32 bytes |
| `PUBLISHING_KEY` | A Fernet key, separate from `JWT_SECRET` | Optional, but without it, rotating `JWT_SECRET` forces every publishing account to reconnect ([PUBLISHING.md](PUBLISHING.md)) |
| `GEMINI_API_KEY` | Restricted to this project in AI Studio | |
| `META_PAGE_ACCESS_TOKEN` | A system-user token with only the permissions in PUBLISHING.md; anyone holding it can post as your Page. Set `META_APP_SECRET` too and turn on *Require app secret* in the Meta app | Checked against Meta when an account is connected |
| `SMTP_PASSWORD` | An app password or API key for a sending-only account, not a person's mailbox password | |
| `PUBLIC_BASE_URL` | The public `https://` address (Instagram fetches media from it; emails link to it) | Instagram connections are refused without `https://` |
| `ALLOW_SIGNUP` | Leave `0` unless you mean to let strangers create workspaces (each can spend your Gemini quota) | Default off |

Keep `.env` readable only by the deploying user (`chmod 600 .env`). It is never copied into the
image.

**Upgrading a stack that used the old default password (`mediaflow`):** Postgres only reads
`POSTGRES_PASSWORD` when its volume is first created, so change the role's password first,
then `.env`:

```bash
NEW=$(openssl rand -hex 24)
docker compose exec postgres psql -U mediaflow -c "ALTER ROLE mediaflow PASSWORD '$NEW'"
echo "POSTGRES_PASSWORD=$NEW" >> .env      # replace any existing line
docker compose up -d                        # API and worker reconnect with the new password
```

## 2. HTTPS and edge rate limits

`compose.proxy.yaml` adds an nginx proxy: it publishes 80 and 443, and stops publishing the API
itself.

| | |
|---|---|
| TLS | TLS 1.2 and 1.3 only; HSTS (1 year), `nosniff`, `X-Frame-Options: DENY`, a strict referrer policy; no version in `Server` |
| HTTP | Redirects to HTTPS, except Let's Encrypt challenges |
| Rate limits, per client address | Sign-in, signup and invitation endpoints: 10 a minute (burst 5). The rest of `/api/`: 10 a second (burst 40). Signed media links are exempt, because a playing video makes many Range requests. Over the limit: `429` |
| Client address | The proxy **replaces** `X-Forwarded-For` with the real address, and the API trusts it (`FORWARDED_ALLOW_IPS=*`, safe because only the proxy can reach the API). A client can't forge its address to dodge the limits |
| Uploads | Streamed to the API (not buffered on the proxy's disk); the API enforces the exact size limits |

These were verified against real nginx with a stub upstream: the redirect, the headers, a burst
of sign-ins turning into `429` after six, signed media never limited, and a forged
`X-Forwarded-For` replaced by the real address.

The API also throttles sign-in itself (`app/api/throttle.py`), so a misconfigured proxy doesn't
leave it open: after `LOGIN_MAX_FAILURES` (10) failures for an account, or
`LOGIN_IP_MAX_FAILURES` (50) from one address, within `LOGIN_WINDOW_S` (15 min), sign-in answers
`429` even with the right password. Signup is limited to `SIGNUP_IP_MAX` (10) an hour per
address. Tests: `tests/integration/test_throttle.py`.

### First certificate

Point the domain's DNS at the host, then, with ports 80 and 443 open:

```bash
export DOMAIN=media.example.com
# 1. One-off: get the certificate (port 80 must be free, so before starting the proxy)
docker compose -f compose.yaml -f compose.proxy.yaml --profile certbot run --rm -p 80:80 \
  certbot certonly --standalone -d "$DOMAIN" --email you@example.com --agree-tos --no-eff-email
# 2. Start everything behind the proxy
docker compose -f compose.yaml -f compose.proxy.yaml up -d --wait
```

### Renewal

Certificates last 90 days. Renew from cron (daily is fine; it only acts within 30 days of
expiry) and reload nginx:

```bash
docker compose -f compose.yaml -f compose.proxy.yaml --profile certbot run --rm \
  certbot renew --webroot -w /var/www/certbot --quiet \
  && docker compose -f compose.yaml -f compose.proxy.yaml exec proxy nginx -s reload
```

Behind a load balancer or a CDN that terminates TLS instead, skip this overlay, keep the API
private to that balancer, set `FORWARDED_ALLOW_IPS` to the balancer's addresses, and configure
equivalent rate limits there.

## 3. Network exposure

Only the proxy (or the API, without the overlay) is published. PostgreSQL, Redis and the
metrics port stay on the private Compose network. Don't publish them; if you need them from
outside, use an SSH tunnel.

## 4. Backups and restore

```bash
scripts/backup.sh                       # -> backups/<UTC timestamp>/
scripts/restore.sh backups/<dir> --yes  # replaces the database and the originals
```

| What | How | Notes |
|---|---|---|
| Database | `pg_dump --format=custom`, a consistent snapshot taken while the app runs | Everything that matters: accounts, workspaces, jobs, drafts and versions, comments, calendar, publications, audit trail |
| Originals (`MEDIA_STORAGE=local`) | `uploads.tar` of the uploads volume | With S3, back up the bucket with the provider's tools (versioning, replication); `storage-check` flags versioning that would undermine deletion ([STORAGE.md](STORAGE.md)) |
| Redis | Not backed up, on purpose | PostgreSQL is the record of truth. After a restore the reconciler re-dispatches any job that was in progress ([JOB_LIFECYCLE.md](JOB_LIFECYCLE.md)) |
| Integrity | `SHA256SUMS`, checked before any restore | |

`restore.sh` refuses to run without `--yes`, verifies the checksums, stops the API, worker and
proxy, restores, and starts them again.

**Restore drill.** Restoring was tested end to end on a throwaway copy of the stack: create an
account's job with a draft and an original, back up, delete every volume, start empty, restore,
and get the same job, draft and original back byte for byte. Repeat that drill after upgrades
and at least quarterly; a backup that has never been restored is a hope, not a backup.

Backups hold everything, including password hashes and encrypted publishing credentials. The
script writes them with `umask 077`; keep them off the host (encrypted object storage), and
retain them according to your deletion policy, because restoring an old backup brings back
originals that retention has since deleted.

`backups/` is in `.gitignore`.

## 5. Before going live

- [ ] `.env` has strong `POSTGRES_PASSWORD`, `JWT_SECRET` and `PUBLISHING_KEY`; `chmod 600 .env`
- [ ] `docker compose -f compose.yaml -f compose.proxy.yaml up -d --wait` behind your domain;
      `https://` loads and `http://` redirects
- [ ] Meta: one real post checked by hand on the Page and on Instagram (PUBLISHING.md, "Meta")
- [ ] Email: a test invitation arrives (not in spam); the sender domain has SPF and DKIM
- [ ] The first admin exists (`make docker-admin EMAIL=...`), and `ALLOW_SIGNUP` is as intended
- [ ] `python -m app.cli storage-check` passes (S3: no public access, encryption, versioning rule)
- [ ] Workspace retention policies reviewed ([STORAGE.md](STORAGE.md))
- [ ] Backups run on a schedule and are copied off the host; one restore drill done
- [ ] Monitoring: `compose.observability.yaml`, or your own Prometheus on the metrics port
      ([OPERATIONS.md](OPERATIONS.md)); alerts reach someone
- [ ] Certificate renewal is scheduled
