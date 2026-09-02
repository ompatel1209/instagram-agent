# Instagram Automation Agent

Fully automatic daily Instagram agent: every day at **9:00 AM IST** it
posts **your own photos and videos** (from your uploads queue) with girly
captions + hashtags — and falls back to a fresh quote post + tip story when
your queue is empty. It runs on GitHub Actions (free) using the
**official Instagram Graph API** (no bots, no ban risk), even while your Mac
is off.

## What it does

```
9:00 AM IST  →  your uploads queue: next unposted photo/video
             →  photo: padded to 4:5 feed + 9:16 Story on a gradient frame
                video: published as a Reel + Story from its cover frame
             →  girly caption + hashtags picked by the file's vibe
9:00 AM IST  →  (queue empty) quote post + tip story, same as before
3:00 PM IST  →  safety re-run: if anything failed in the morning, it retries
                only the missing steps — never double-posts
```

## Your photos & videos (the uploads queue)

Drop your files into the `uploads/` folder in this project on your Mac, then
tell the agent (me) — I queue them to GitHub. Rules:

- **Name files by vibe** so the caption matches: `selfie1.jpg`,
  `attitude2.mp4`, `cute3.jpg`, `ootd4.jpg`, `travel5.jpg`, `selflove6.jpg`.
  Unknown names (e.g. `IMG_1234.jpg`) still post — with a general caption.
- **One file posts per day**, in name order, never repeating a file. Add
  10 files → the next 10 days are covered.
- **Photos**: jpg / jpeg / png / heic / webp — posted uncropped, letterboxed
  on your gradient so any orientation fills the frame.
- **Videos**: mp4 / mov / m4v, vertical 9:16, 3s–15min — posted as a
  **Reel** (+ Story from the video's cover frame).
- Files live on the private-to-you `media` branch; the local `uploads/`
  folder is just your staging area (gitignored on main).

## One-time setup (do this once; ~15 minutes)

### 1. Convert your Instagram to Professional (2 min)
Instagram app → Profile → Menu (☰) → **Settings and privacy** → **Account type
and tools** → **Switch to professional account** → choose **Creator** (or
Business). Your followers, posts, and handle stay exactly the same.

### 2. Create the Meta app + get your token (5 min)
1. Go to <https://developers.facebook.com> → Log in with Facebook →
   **Get started** / **My Apps** → **Create app** → type **"Business"** → Create.
2. In the app dashboard, find/set up the **Instagram API with Instagram Login**
   product. In **Basic settings**, copy your **Instagram app id**.
3. Open your app's **Basic info > Instagram app** settings — you'll see a
   **Generate token** option (or use the API setup wizard). Log in with your
   **Instagram username and password** (not Facebook), approve the
   `instagram_business_basic` + `instagram_business_content_publish`
   permissions, and copy the **long-lived access token** shown. It lasts
   ~60 days and the workflow auto-refreshes it.
4. Also copy your **Instagram User ID** (shown on the same screen, or run
   step 5's test below and it will resolve automatically via `user_id`).

> If your app shows an **API setup checklist**, the "Generate token" flow is
> under **Instagram API with Instagram Login → API setup**.

### 3. Create a GitHub repo + add secrets (5 min)
1. Create a GitHub account at <https://github.com> if you don't have one.
2. Create a **new public repository** (name it e.g. `instagram-agent`)
   — do NOT initialize with a README.
3. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**, add these three:
   - `IG_ACCESS_TOKEN` — the token from step 2
   - `IG_USER_ID` — your Instagram user id (optional but recommended)
   - `GH_PAT` — a personal access token: GitHub → Settings → Developer
     settings → Personal access tokens → **Fine-grained** → Generate; select
     **only this repo**, permission **Contents: Read and write**. This lets
     the workflow push media/state and refresh the token secret.
4. Tell me your `owner/repo` and I'll wire `config.json` and push the code
   (or edit `config.json` yourself: set `repo_owner`, `repo_name`, `handle`).

### 4. Test it
1. In the repo: **Actions** tab → **Daily Instagram post + story** →
   **Run workflow** → Run.
2. Watch the run turn green, then check Instagram — the test post + story
   should appear within minutes.
3. That's it. From tomorrow, 9:00 AM IST posts happen automatically.

## Preview on your Mac anytime

```bash
cd Instagram
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.main --dry-run
open preview/          # today's post + story images
```

## How it never double-posts
`state.json` records every successful step per day. The 3 PM re-run (and any
manual re-run) reads it and completes **only** what's missing. If the feed
post succeeded but the story failed, only the story is retried.

## Costs
- GitHub Actions: **free** (public repo, well under the free minutes)
- Instagram API: **free**
- No LLM APIs — content comes from a curated bank of 120 quotes + 60 tips
  (~6 months of unique daily posts before anything repeats).

## Troubleshooting
- **Run skipped/failed**: Actions tab → click the failed run → read the
  [ig-agent] log lines. The 3 PM safety re-run retries automatically.
- **Token expired** (after ~60 days if auto-refresh couldn't write back):
  re-generate from the Meta app dashboard and update the
  `IG_ACCESS_TOKEN` secret.
- **Want a different look?** Edit `palette` colors in `config.json`
  (RGB arrays), or ask me to restyle the templates.
- **Caption not matching the photo?** Rename the file by its vibe
  (`attitude7.jpg` instead of `IMG_1234.jpg`) — the vibe word before the
  number picks the caption set (see `content/captions.json`).
- **Want to repost an old upload?** Remove its filename from
  `posted_files` in `state.json` (kept on the `media` branch) and re-queue.
