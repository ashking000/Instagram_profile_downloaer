# Instagram Profile Downloader

A simple command-line script that downloads **all images and videos** from one or
more Instagram profiles into a folder named after each profile. It is built on
top of the bundled [`instaloader`](https://instaloader.github.io/) package and
adds basic **bot-detection handling** so it fails gracefully instead of hammering
Instagram when it gets rate-limited.

---

## Requirements

- Python 3.9 or newer
- Dependencies from `requirements.txt`

Install the dependencies:

```bash
pip install -r requirements.txt
```

This installs:

| Package           | Purpose                                                        |
| ----------------- | -------------------------------------------------------------- |
| `requests`        | Core HTTP library used by `instaloader`.                       |
| `browser_cookie3` | Optional. Lets you reuse a browser login via `--import-browser`. |

---

## Quick start

```bash
python download_profile.py <username>
```

Media is saved to a new folder named after the profile, e.g. `./instagram/`.

```bash
# One profile
python download_profile.py instagram

# Several profiles at once
python download_profile.py nasa natgeo

# Log in first (needed for private profiles you follow)
python download_profile.py someone --login YOUR_USERNAME

# Reuse an existing browser session (most human-looking)
python download_profile.py someone --import-browser firefox
```

---

## Options

| Option                    | Description                                                                                     |
| ------------------------- | ----------------------------------------------------------------------------------------------- |
| `profiles`                | One or more Instagram usernames to download (required).                                         |
| `--login USER`            | Log in as `USER` first. You are prompted for the password on the terminal (2FA supported).      |
| `--import-browser BROWSER`| Import an existing Instagram login from your browser cookies (`firefox`, `chrome`, `edge`, ...). |
| `--dest FOLDER`           | Base folder to download into. Defaults to the current directory.                                |
| `--no-videos`             | Download images only, skip videos.                                                              |
| `--no-images`             | Download videos only, skip images.                                                              |
| `--slow`                  | Extra-cautious mode with longer, randomized delays to reduce the chance of being flagged.       |
| `-h`, `--help`            | Show the built-in help and exit.                                                                |

---

## Logging in

Instagram now requires being logged in for most profile scraping. If you see a
`Login required` message, authenticate with one of these methods:

### 1. Password login

```bash
python download_profile.py someone --login YOUR_USERNAME
```

You are prompted for your password (and a 2FA code if enabled). The session is
saved so later runs skip the login step.

### 2. Import a browser session (recommended)

If you are already logged in to `instagram.com` in your browser, reuse that
session. This looks the most "human" to Instagram:

```bash
python download_profile.py someone --import-browser firefox
```

Supported browsers: `firefox`, `chrome`, `chromium`, `edge`, `opera`, `brave`,
`vivaldi`, `safari`. Requires `browser_cookie3` (installed via
`requirements.txt`).

> Private profiles only work if you log in with an account that **follows** them.

---

## Bot detection

Instagram actively tries to detect automated scraping. This script takes several
steps to stay under the radar and to react cleanly when it does get flagged:

- **Human-like pacing** – a custom rate controller adds randomized pauses
  between requests, and longer ones in `--slow` mode.
- **Session reuse** – it logs in once and caches the session instead of
  authenticating repeatedly.
- **Realistic user agent** – requests use a normal desktop-browser identifier.
- **Graceful stop** – if Instagram responds with `429 Too Many Requests`,
  `403 Forbidden`, or a `checkpoint`/`challenge` page (the tell-tale signs of
  being flagged), the script stops with a clear message instead of looping.

If you get flagged:

1. **Stop and wait hours, not minutes** before trying again.
2. Avoid using the Instagram app on the same IP/network in the meantime.
3. Retry in `--slow` mode:

   ```bash
   python download_profile.py someone --login YOUR_USERNAME --slow
   ```

> No client-side technique can guarantee you won't be flagged. Instagram's
> detection is server-side and aggressive. These measures reduce the risk and
> make failures graceful.

---

## Output layout

```
<dest>/
└── <username>/
    ├── <profile picture>
    ├── 2024-01-01_12-00-00_UTC.jpg
    ├── 2024-01-02_09-30-00_UTC.mp4
    └── ...
```

Captions, comments, and JSON metadata are **not** saved, only media files.

---

## Examples

```bash
# Images only, into a custom folder
python download_profile.py natgeo --no-videos --dest downloads

# Cautious download of a private profile you follow
python download_profile.py a_friend --login YOUR_USERNAME --slow

# Download several public profiles using your browser session
python download_profile.py nasa spacex --import-browser chrome
```

---

## Responsible use

Downloading another user's media may be subject to Instagram's Terms of Service
and copyright law. Only download content you have the right to, and use this
tool responsibly.

---

## Credits

This project is built on top of [**instaloader**](https://instaloader.github.io/),
an excellent library for Instagram profile downloads. Special thanks to the
instaloader maintainers for their work on Instagram automation and scraping.
