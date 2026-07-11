#!/usr/bin/env python3
"""
Simple Instagram profile downloader with basic bot-detection handling.

Give it a profile name and it downloads all images and videos from that
profile into a folder named after the profile.

Built on top of the `instaloader` package that lives in this repo.

Usage
-----
    python download_profile.py <username> [<username> ...]

Examples
--------
    python download_profile.py instagram
    python download_profile.py nasa natgeo
    python download_profile.py someone --login YOUR_USERNAME
    python download_profile.py someone --import-browser firefox

Options
-------
    --login YOUR_USERNAME   Log in first (needed for private profiles you
                            follow, and generally more reliable). You will be
                            asked for the password on the terminal.
    --import-browser NAME   Import an existing login session from your browser
                            cookies (firefox, chrome, edge, ...). Requires the
                            optional `browser_cookie3` package. This looks the
                            most "human" to Instagram.
    --no-videos             Download images only, skip videos.
    --no-images             Download videos only, skip images.
    --dest FOLDER           Base folder to download into (default: current dir).
    --slow                  Extra-cautious mode: longer, more randomized delays
                            to reduce the chance of being flagged as a bot.

Notes
-----
- Instagram requires being logged in for most profile scraping. If you get a
  "Login required" error, re-run with --login YOUR_USERNAME.
- Downloading someone else's media may be subject to Instagram's Terms of
  Service and copyright law. Use responsibly.

Bot detection
-------------
Instagram actively tries to detect automated scraping. This script does a few
things to stay under the radar and to react gracefully when it gets flagged:
  * A custom rate controller adds randomized, human-like pauses between
    requests (and longer ones in --slow mode).
  * It reuses a saved login session instead of logging in repeatedly.
  * It detects the tell-tale signals of being flagged (HTTP 429 Too Many
    Requests, 403 Forbidden, "checkpoint"/"challenge" responses) and stops with
    a clear explanation instead of hammering the server.
There is no way to guarantee you won't be flagged. If it happens, wait a while
(hours, not minutes), avoid using the Instagram app on the same IP meanwhile,
and try again in --slow mode.
"""

import argparse
import random
import sys
import time

import instaloader
from instaloader import Instaloader, Profile, RateController
from instaloader.exceptions import (
    AbortDownloadException,
    BadCredentialsException,
    ConnectionException,
    LoginRequiredException,
    PrivateProfileNotFollowedException,
    ProfileNotExistsException,
    QueryReturnedBadRequestException,
    QueryReturnedForbiddenException,
    TooManyRequestsException,
    TwoFactorAuthRequiredException,
)

# A realistic desktop browser user agent so requests don't look like a script.
HUMAN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class BotDetectedException(Exception):
    """Raised when Instagram appears to have flagged this session as a bot."""


class HumanlikeRateController(RateController):
    """
    Rate controller that behaves more like a human and reacts to bot detection.

    On top of instaloader's built-in rate limiting it:
      * adds a small randomized delay before every request (jitter), so the
        traffic pattern is not perfectly regular;
      * counts how often Instagram answers with 429 (Too Many Requests) and, if
        that keeps happening, treats it as bot detection and aborts instead of
        looping forever.
    """

    def __init__(self, context, slow: bool = False):
        super().__init__(context)
        self._slow = slow
        self._consecutive_429 = 0
        # Abort after this many back-to-back rate-limit hits.
        self._max_consecutive_429 = 3

    def sleep(self, secs: float):
        # Add human-like jitter on top of whatever wait instaloader requested.
        if self._slow:
            secs += random.uniform(2.0, 6.0)
        else:
            secs += random.uniform(0.5, 2.0)
        time.sleep(secs)

    def wait_before_query(self, query_type: str) -> None:
        # A tiny idle pause before each query mimics a human browsing.
        jitter = random.uniform(1.5, 5.0) if self._slow else random.uniform(0.3, 1.5)
        time.sleep(jitter)
        super().wait_before_query(query_type)
        # A successful (non-429) query resets the strike counter.
        self._consecutive_429 = 0

    def handle_429(self, query_type: str) -> None:
        self._consecutive_429 += 1
        if self._consecutive_429 >= self._max_consecutive_429:
            raise BotDetectedException(
                "Instagram returned 'Too Many Requests' {} times in a row. "
                "You have most likely been rate-limited / flagged as a bot. "
                "Stop now, wait a few hours, and retry with --slow.".format(
                    self._consecutive_429
                )
            )
        super().handle_429(query_type)


def build_loader(
    dest: str, download_images: bool, download_videos: bool, slow: bool
) -> Instaloader:
    """Create an Instaloader configured to save media under <dest>/<profile>."""
    return Instaloader(
        # Put files into: <dest>/<profile name>/
        dirname_pattern=(dest.rstrip("/\\") + "/{target}") if dest else "{target}",
        user_agent=HUMAN_USER_AGENT,
        download_pictures=download_images,
        download_videos=download_videos,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="",  # don't write caption .txt files
        max_connection_attempts=1,  # don't hammer on failure; let us handle it
        rate_controller=lambda ctx: HumanlikeRateController(ctx, slow=slow),
    )


def looks_like_bot_detection(exc: Exception) -> bool:
    """Heuristic: does this error look like Instagram flagging us as a bot?"""
    if isinstance(exc, (BotDetectedException, TooManyRequestsException)):
        return True
    text = str(exc).lower()
    signals = ("429", "too many requests", "checkpoint", "challenge",
               "please wait a few minutes", "429 - too many requests")
    return any(s in text for s in signals)


def do_login(loader: Instaloader, login_user: str) -> None:
    """Interactive login, reusing a saved session when available."""
    try:
        loader.load_session_from_file(login_user)
        print(f"Loaded saved session for {login_user}.")
        return
    except FileNotFoundError:
        pass

    try:
        loader.interactive_login(login_user)  # prompts for password on terminal
        loader.save_session_to_file()  # cache session for next time
        print(f"Logged in as {login_user} (session saved for next time).")
    except TwoFactorAuthRequiredException:
        code = input("Enter the 2FA code from your authenticator app: ").strip()
        loader.two_factor_login(code)
        loader.save_session_to_file()
        print(f"Logged in as {login_user} with 2FA (session saved).")
    except BadCredentialsException:
        sys.exit("Login failed: wrong username or password.")


def import_browser_session(loader: Instaloader, browser: str) -> None:
    """Import an existing Instagram login session from the browser's cookies."""
    try:
        import browser_cookie3  # type: ignore
    except ImportError:
        sys.exit(
            "The --import-browser option needs the 'browser_cookie3' package.\n"
            "Install it with:  pip install browser_cookie3"
        )

    getter = getattr(browser_cookie3, browser.lower(), None)
    if getter is None:
        sys.exit(
            f"Unknown browser '{browser}'. Try one of: firefox, chrome, chromium, "
            "edge, opera, brave, vivaldi, safari."
        )

    print(f"Importing Instagram session cookies from {browser}...")
    # Build a {name: value} dict of instagram.com cookies (mirrors instaloader CLI).
    cookies = {c.name: c.value for c in getter() if "instagram" in c.domain}
    if not cookies:
        sys.exit(
            f"No Instagram cookies found in {browser}. Make sure you are logged in "
            "to instagram.com in it, then try again."
        )

    loader.context.update_cookies(cookies)
    username = loader.test_login()
    if not username:
        sys.exit(
            "Found cookies but the session is not valid. Log in to instagram.com in "
            f"{browser} and try again."
        )
    loader.context.username = username
    loader.save_session_to_file()
    print(f"Imported session for {username} from {browser} (session saved).")


def download_one(loader: Instaloader, username: str) -> bool:
    """Download all posts of a single profile. Returns True on success."""
    print(f"\n=== Downloading profile: {username} ===")
    try:
        profile = Profile.from_username(loader.context, username)
    except ProfileNotExistsException:
        print(f"  Profile '{username}' does not exist. Skipping.")
        return False
    except LoginRequiredException:
        print("  Login required to view this profile. Re-run with --login YOUR_USERNAME.")
        return False

    print(f"  Full name : {profile.full_name}")
    print(f"  Posts     : {profile.mediacount}")
    print(f"  Private   : {profile.is_private}")

    loader.download_profiles(
        {profile},
        profile_pic=True,
        posts=True,
        tagged=False,
        igtv=False,
        highlights=False,
        stories=False,
        raise_errors=True,
    )
    print(f"  Done with {username}.")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download all images and videos from Instagram profile(s).",
    )
    parser.add_argument("profiles", nargs="+", help="Instagram username(s) to download.")
    parser.add_argument("--login", metavar="USER", help="Log in as this user first.")
    parser.add_argument(
        "--import-browser",
        metavar="BROWSER",
        help="Import login session from a browser (firefox, chrome, edge, ...).",
    )
    parser.add_argument("--dest", default="", help="Base download folder (default: current dir).")
    parser.add_argument("--no-videos", action="store_true", help="Skip videos.")
    parser.add_argument("--no-images", action="store_true", help="Skip images.")
    parser.add_argument(
        "--slow",
        action="store_true",
        help="Extra-cautious mode with longer randomized delays (avoids bot flags).",
    )
    args = parser.parse_args()

    download_images = not args.no_images
    download_videos = not args.no_videos
    if not download_images and not download_videos:
        sys.exit("Nothing to download: you passed both --no-images and --no-videos.")

    loader = build_loader(args.dest, download_images, download_videos, args.slow)

    if args.import_browser:
        import_browser_session(loader, args.import_browser)
    if args.login:
        do_login(loader, args.login)

    ok = 0
    for username in args.profiles:
        try:
            if download_one(loader, username):
                ok += 1
        except (BotDetectedException, AbortDownloadException) as exc:
            sys.exit(f"\nStopped: {exc}")
        except (
            TooManyRequestsException,
            QueryReturnedForbiddenException,
            QueryReturnedBadRequestException,
        ) as exc:
            sys.exit(
                f"\nStopped: Instagram appears to have flagged this session as a bot "
                f"({exc}). Wait a few hours and retry with --slow."
            )
        except PrivateProfileNotFollowedException:
            print("  This profile is private and you don't follow it. Cannot download.")
        except LoginRequiredException:
            print("  Login required. Re-run with --login YOUR_USERNAME.")
        except ConnectionException as exc:
            if looks_like_bot_detection(exc):
                sys.exit(
                    f"\nStopped: this looks like bot detection ({exc}). "
                    f"Wait a few hours and retry with --slow."
                )
            print(f"  Connection error for '{username}': {exc}")

    total = len(args.profiles)
    print(f"\nFinished: {ok}/{total} profile(s) downloaded successfully.")
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
