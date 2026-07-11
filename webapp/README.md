# Instagram Downloader — Web App

A local, browser-style web app for downloading Instagram profiles. It wraps the
bundled `instaloader` package with a Flask backend and a simple gallery UI.

Features:

- **Browser-style UI** — type a username in the "address bar" and hit Download.
- **Two download engines** (pick from the dropdown):
  - **Instaloader** — fast API-based downloader.
  - **Real browser** — drives an actual Chromium browser via Playwright, with a
    **desktop or mobile** user agent. Because Instagram's own JavaScript runs and
    makes the requests, this has the **lowest bot-detection footprint** and is
    the best option when you keep hitting `429 Too Many Requests`.
- **Log in** so you can also grab **private profiles** you follow. Public
  profiles work with or without login.
- **Live progress** while the profile downloads in the background.
- **Gallery** of every image and video, with previews (lightbox).
- **Select** individual items or **select all**, filter by images/videos.
- **Download** a single file, or **bulk download selected / all as a ZIP**.
- Media is saved on the **local server** under `webapp/downloads/<username>/`.

## Setup

From the project root:

```bash
pip install -r requirements.txt
```

For the **Real browser** engine, also install the Chromium binary once:

```bash
python -m playwright install chromium
```

## Run

```bash
python webapp/app.py
```

Then open <http://127.0.0.1:5000> in your browser.

## How to use

1. Pick a **download engine** in the header dropdown:
   - **Instaloader** (default) — fast, but Instagram rate-limits its API quickly.
   - **Real browser** — slower but far more reliable. Tick **Mobile** to use an
     iPhone user agent instead of desktop Chrome.
2. **Log in** if you need private profiles (or to reduce 429 errors):
   - *Password / cookie import* (for the Instaloader engine) via **Log in**.
   - *Open browser to log in* (for the Real browser engine) — this launches a
     real Chromium window; log in there like a normal person (2FA works), and
     the session is saved for future downloads.
3. Type a profile username in the address bar and click **Download**.
4. Watch the progress bar. When it finishes, the gallery fills with the media.
5. Preview items, tick the ones you want (or **Select all**), then use
   **Download selected (ZIP)** — or **Download all (ZIP)**, or the per-item
   **Download** button for a single file.

## Which engine should I use?

| Situation | Recommended engine |
| --- | --- |
| Quick grab, no rate-limit issues | Instaloader |
| Getting `429 Too Many Requests` | Real browser |
| Want it to look like a phone | Real browser + **Mobile** |
| Private profile you follow | Either, after logging in |

The Real browser engine works by opening the profile in Chromium, letting
Instagram's own frontend fetch the posts, and collecting media URLs from those
responses while scrolling. It downloads each file using the browser's own
session, so the traffic looks like ordinary browsing.

## Where files go

```
webapp/
├── downloads/
│   └── <username>/        # images + videos + profile picture
└── sessions/
    └── session-<user>     # cached login (contains auth cookies)
```

Both folders are git-ignored. The `sessions/` files hold login cookies, treat
them like passwords and don't share them.

## Security notes

- This server has **no authentication of its own** and it accepts Instagram
  credentials. Run it **only on your local machine** (it binds to `127.0.0.1`)
  and never expose it to the public internet.
- Credentials are sent straight to Instagram to create a login session; they are
  not written to disk in plain text. The resulting session cookie is cached in
  `webapp/sessions/` so you don't have to log in every time.

## Limitations & responsible use

- Instagram's bot detection is server-side and aggressive. The pacing here
  reduces the risk but can't eliminate it. If you get rate-limited, wait a few
  hours before trying again.
- Download jobs and login sessions are kept in memory and reset when the server
  restarts (downloaded files on disk remain).
- Downloading another user's media may be subject to Instagram's Terms of
  Service and copyright law. Only download content you have the right to.
```
