# Welcome to the MediaFlow demo

The demo starts automatically and opens in a **new browser tab**. If the tab didn't open,
open the **Ports** tab at the bottom of the window and click the globe icon next to
**MediaFlow demo (8082)**. If your browser blocked the pop-up, allow it and reload.

## Sign in

The sign-in page has one-click **Continue as …** buttons, or use:

| Role | Email | Password |
|---|---|---|
| Admin | `demo-admin@example.com` | `demo-password-123` |
| Manager | `demo-manager@example.com` | `demo-password-123` |
| Member | `demo-member@example.com` | `demo-password-123` |

## Try the whole workflow (about 2 minutes)

1. **Continue as Member** and upload any photo in **Library**. A few seconds later it shows
   **In review**, with Instagram, TikTok and Facebook drafts.
2. Sign out, **Continue as Manager**, open **Review**, and approve the draft.
3. Sign out, **Continue as Member**, open the draft, and schedule it.
4. Open **Calendar** to see it planned.

Drafts are written by an offline stand-in, not Gemini. No API key is used.

**About the demo link:** the demo port is set to *public*, so it opens in any browser without
GitHub's port sign-in, which some browsers block with "HTTP ERROR 401". Anyone with this
Codespace's link can open the demo, so share it only if you mean to. The link is long and
random, logins are signed with a key unique to this Codespace, and it holds only demo data.
To make it private, right-click port 8082 in the **Ports** tab and choose
**Port Visibility → Private**.

## Useful commands (in the terminal below)

```bash
bash .devcontainer/start-demo.sh --restart   # restart the demo
tail -f /tmp/mediaflow-demo.log              # watch the server log
python3 scripts/demo_check.py http://localhost:8082   # run the workflow check from the terminal
make done                                    # lint + all Python tests (Redis is already running)
make web-test                                # web app tests
```

Stop the Codespace when you're done (**Codespaces** menu, then **Stop current codespace**) to
save your free hours. More detail is in the [README](../README.md).
