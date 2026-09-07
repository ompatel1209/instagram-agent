# Feature 5 — Instant replies (Cloudflare Worker + Meta webhook)

The hourly `engage.yml` sweep answers comments/DMs at most ~67 minutes late.
This worker closes that gap: Meta pushes each comment/DM event the second it
happens, the worker replies via the same NVIDIA NIM voice (`src/ai.py`) and
the Instagram Graph API — seconds, not minutes.

```
fan comments / DMs ──> Meta webhook ──> this worker (Cloudflare, free tier)
                                              │  NIM (muse-glimmer-30b)
                                              ▼
                                     reply via Graph API ──> Instagram
        (hourly engage.yml sweep stays running as the missed-event safety net)
```

## Why it's safe: the no-sync design

The worker holds **no state** — no KV, no database, no GitHub write-back,
no PAT. Both systems ask Instagram the same question before replying to a
comment: *"does our handle already appear among this comment's replies?"*
(`GET /{comment-id}/replies`). If yes — whether the reply came from the
worker, the hourly sweep, or the owner's phone — skip. The sweep additionally
pre-marks such comments in `state.json` so future sweeps cost zero GETs.

DMs are race-safe by construction: both sides only answer a thread whose
newest message is incoming. The worker's instant reply makes the newest
message ours, so the sweep never touches that thread again; the worker
fetches the thread's history itself so instant conversations keep memory.

A poison event can't wedge anything: the worker always returns 200 (Meta
retries non-200 forever), and any handler failure just means the hourly
sweep catches that comment/DM later.

## Deploy (one-time, ~10 minutes)

Requires Node 18+. From this `webhook/` directory:

```bash
npm install -g wrangler        # or: npx wrangler … for every command below
wrangler login                 # opens a browser; approve Cloudflare access
wrangler deploy                # prints https://instagram-agent-webhook.<you>.workers.dev
```

Then set the secrets (each prompts for the value; never put them in files —
this repo is public):

```bash
wrangler secret put IG_USER_ID           # 17841460324330889
wrangler secret put IG_ACCESS_TOKEN      # the same long-lived token GitHub Actions uses
wrangler secret put NVIDIA_API_KEY       # the NIM key (same one Actions uses)
wrangler secret put WEBHOOK_VERIFY_TOKEN # any long random string you invent — reuse it in the Meta dashboard
wrangler secret put APP_SECRET           # OPTIONAL: Meta app's App Secret — enables request-signature verification
```

Optional plain config (defaults are baked into `worker.js`):
`IG_HANDLE` (`whoisaaniiiya`), `NO_REPLY_USERS` (comma-separated), and
`REPLY_BANK_URL` (defaults to the repo's `content/replies.json` on the
`main` branch — the same bank the hourly sweep reads, so instant and hourly
fallback replies come from one pool).

**Note:** the Instagram token expires (~60 days). When you refresh it in
GitHub Secrets for the Actions workflows, also re-run:
`wrangler secret put IG_ACCESS_TOKEN`.

## Subscribe Meta to the webhook (one-time)

1. Go to <https://developers.facebook.com> → your app (the same one that
   provides the Instagram login/token).
2. In the app dashboard, open **Webhooks** (left sidebar).
3. Pick the **Instagram** object. (If you don't see it, the app must be
   linked to the professional account — which it already is, since its
   token publishes daily.)
4. **Callback URL**: `https://instagram-agent-webhook.<you>.workers.dev/webhook`
   — the exact URL `wrangler deploy` printed.
5. **Verify token**: the exact string you set as `WEBHOOK_VERIFY_TOKEN`.
6. Click **Verify and save** — Meta sends the GET handshake; the worker
   echoes `hub.challenge` and it turns green.
7. **Subscribe to fields**: `comments` and `messages`. Save.
8. If the app is in development mode, also add **App Roles → your own
   account → Administrator/Tester is already enough** for your own
   account's events. For events from *other* users, the app must be
   **Live** (App Review) — see "Known limits" below.

## Test it

```bash
# 1. The handshake (Meta's check, done by hand):
curl "https://<worker-url>/webhook?hub.mode=subscribe&hub.verify_token=<YOUR_TOKEN>&hub.challenge=hello123"
# → hello123

# 2. A forged comment event (verify the reply lands on the account):
curl -X POST "https://<worker-url>/webhook" \
  -H "Content-Type: application/json" \
  -d '{"object":"instagram","entry":[{"id":"17841460324330889","changes":[{"field":"comments","value":{"id":"<real-comment-id>","text":"love your posts!!","from":{"id":"123","username":"somefan"}}}]}]}'
```

Then check the comment on the phone — the reply should appear in seconds.
`wrangler tail` streams the worker's logs live while you test.

## Known limits

- **Non-follower comments on some posts** stay unfixable — that's an
  Instagram API surface restriction, not latency.
- **Webhook events fire for *new* activity only.** The hourly sweep remains
  the safety net for anything Meta doesn't deliver.
- Free tier: 100k requests/day — engagement volume uses a rounding error
  of that. NIM's 200k tokens/day cap is shared with the sweep, as before.
