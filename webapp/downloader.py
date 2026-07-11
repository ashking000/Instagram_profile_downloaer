"""
Backend download logic for the web app.

Wraps the bundled `instaloader` package with:
  * a human-like rate controller to keep bot-detection low,
  * login / 2FA helpers,
  * a per-post download loop that reports progress,
  * media listing and simple type detection.

This module is deliberately framework-agnostic: it knows nothing about Flask.
"""

import os
import threading
import time
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import instaloader
from instaloader import Instaloader, Profile, RateController
from instaloader.exceptions import (
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

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm"}


class BotDetectedException(Exception):
    """Raised when Instagram appears to have flagged this session as a bot."""


class HumanlikeRateController(RateController):
    """Rate controller that paces requests like a human and reacts to 429s."""

    def __init__(self, context, slow: bool = False):
        super().__init__(context)
        self._slow = slow
        self._consecutive_429 = 0
        self._max_consecutive_429 = 4
        self._last_429_at = 0.0

    def sleep(self, secs: float):
        secs += random.uniform(2.0, 6.0) if self._slow else random.uniform(0.5, 2.0)
        time.sleep(secs)

    def wait_before_query(self, query_type: str) -> None:
        jitter = random.uniform(1.5, 5.0) if self._slow else random.uniform(0.3, 1.5)
        time.sleep(jitter)
        super().wait_before_query(query_type)

    def handle_429(self, query_type: str) -> None:
        now = time.monotonic()
        # Only count 429s that happen close together as a "streak"; a lone 429
        # during a long, otherwise-healthy download should not abort everything.
        if now - self._last_429_at > 180:
            self._consecutive_429 = 0
        self._consecutive_429 += 1
        self._last_429_at = now

        if self._consecutive_429 >= self._max_consecutive_429:
            raise BotDetectedException(
                "Instagram kept returning 429 (Too Many Requests). Your IP or account "
                "is rate-limited right now. Stop, wait a few hours, and try again - "
                "ideally logged in via an imported browser session (see 'Use browser "
                "login')."
            )

        # For a pre-existing rate limit instaloader's own wait computes ~0s (it only
        # knows about requests it made this run), so impose a real escalating backoff
        # to actually give the server time to cool down before retrying.
        base = 60 if self._slow else 30
        backoff = base * self._consecutive_429
        self._context.log(
            "Rate limited (429). Waiting {}s before retry [{}/{}]...".format(
                backoff, self._consecutive_429, self._max_consecutive_429
            )
        )
        time.sleep(backoff)
        super().handle_429(query_type)


@dataclass
class DownloadJob:
    """Tracks the state of a single background profile download."""

    job_id: str
    profile: str
    state: str = "pending"  # pending | running | done | error
    total: int = 0
    done: int = 0
    message: str = ""
    full_name: str = ""
    is_private: bool = False
    error: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def as_dict(self) -> dict:
        with self.lock:
            return {
                "job_id": self.job_id,
                "profile": self.profile,
                "state": self.state,
                "total": self.total,
                "done": self.done,
                "message": self.message,
                "full_name": self.full_name,
                "is_private": self.is_private,
                "error": self.error,
                "percent": int(self.done / self.total * 100) if self.total else 0,
            }


class DownloaderSession:
    """
    Holds one logged-in (or anonymous) Instaloader instance plus its jobs.

    One of these exists per browser session.
    """

    def __init__(self, download_root: str, session_dir: str, slow: bool = False,
                 browser_dir: Optional[str] = None):
        self.download_root = download_root
        self.session_dir = session_dir
        self.slow = slow
        self.username: Optional[str] = None
        self._pending_2fa = False
        self.jobs: Dict[str, DownloadJob] = {}
        os.makedirs(download_root, exist_ok=True)
        os.makedirs(session_dir, exist_ok=True)
        self.loader = self._build_loader()

        # --- Real browser engine (Playwright) state ---
        self.browser_dir = browser_dir or os.path.join(session_dir, "browser_profile")
        self._browser_lock = threading.Lock()  # persistent context can't open twice
        self.browser_login_running = False
        self.browser_login_status = ""
        self.browser_logged_in = False

    # ---- loader setup -------------------------------------------------
    def _build_loader(self) -> Instaloader:
        return Instaloader(
            dirname_pattern=os.path.join(self.download_root, "{target}"),
            user_agent=HUMAN_USER_AGENT,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            # Allow several attempts so the rate controller's escalating 429
            # backoff can actually run instead of failing on the first 429.
            max_connection_attempts=5,
            request_timeout=30.0,
            rate_controller=lambda ctx: HumanlikeRateController(ctx, slow=self.slow),
        )

    # ---- auth ---------------------------------------------------------
    @property
    def is_logged_in(self) -> bool:
        return bool(self.username)

    def _session_file(self, user: str) -> str:
        return os.path.join(self.session_dir, f"session-{user}")

    def login(self, user: str, password: str) -> str:
        """Returns 'ok', '2fa', or raises on hard failure."""
        # Reuse a cached session if we have one.
        session_file = self._session_file(user)
        if os.path.exists(session_file):
            try:
                self.loader.load_session_from_file(user, session_file)
                if self.loader.test_login() == user:
                    self.username = user
                    return "ok"
            except Exception:
                pass  # fall through to a fresh login

        try:
            self.loader.login(user, password)
            self.username = user
            self.loader.save_session_to_file(session_file)
            return "ok"
        except TwoFactorAuthRequiredException:
            self._pending_2fa = True
            self._pending_user = user
            return "2fa"
        except BadCredentialsException as exc:
            raise ValueError("Wrong username or password.") from exc

    def import_browser_session(self, browser: str) -> str:
        """
        Reuse an existing instagram.com login from the given browser's cookies.

        A real browser session is far less likely to be hit with 429s than a
        fresh programmatic login. Returns the logged-in username.
        """
        try:
            import browser_cookie3  # type: ignore
        except ImportError as exc:
            raise ValueError(
                "browser_cookie3 is not installed. Run: pip install browser_cookie3"
            ) from exc

        getter = getattr(browser_cookie3, browser.lower(), None)
        if getter is None:
            raise ValueError(
                f"Unknown browser '{browser}'. Try: firefox, chrome, chromium, edge, "
                "opera, brave, vivaldi, safari."
            )

        try:
            raw = getter()
        except Exception as exc:  # locked cookie DB, browser running, etc.
            raise ValueError(
                f"Could not read cookies from {browser}: {exc}. Close the browser and "
                "try again, or make sure you are logged in to instagram.com in it."
            ) from exc

        cookies = {c.name: c.value for c in raw if "instagram" in c.domain}
        if not cookies:
            raise ValueError(
                f"No Instagram cookies found in {browser}. Log in to instagram.com in "
                "that browser first, then retry."
            )

        self.loader.context.update_cookies(cookies)
        username = self.loader.test_login()
        if not username:
            raise ValueError(
                f"Found cookies in {browser} but the session is not valid. Log in to "
                "instagram.com there and try again."
            )
        self.loader.context.username = username
        self.username = username
        self.loader.save_session_to_file(self._session_file(username))
        return username

    def complete_2fa(self, code: str) -> str:
        if not self._pending_2fa:
            raise ValueError("No two-factor login is in progress.")
        self.loader.two_factor_login(code)
        self.username = self._pending_user
        self._pending_2fa = False
        self.loader.save_session_to_file(self._session_file(self.username))
        return "ok"

    def logout(self) -> None:
        self.username = None
        self._pending_2fa = False
        self.loader = self._build_loader()

    # ---- real browser (Playwright) login ------------------------------
    def _make_browser_engine(self, mobile: bool):
        from browser_downloader import BrowserEngine  # lazy: playwright optional
        return BrowserEngine(self.browser_dir, mobile=mobile)

    def open_browser_login(self, mobile: bool = False) -> None:
        """Open a real, visible browser window for the user to log in. Async."""
        if self.browser_login_running:
            return

        def worker():
            self.browser_login_running = True
            self.browser_login_status = "Launching browser..."
            try:
                with self._browser_lock:
                    engine = self._make_browser_engine(mobile)
                    ok = engine.open_login(
                        wait_seconds=300,
                        on_status=lambda m: setattr(self, "browser_login_status", m),
                    )
                self.browser_logged_in = ok
                if ok:
                    self.browser_login_status = "Logged in via browser."
            except Exception as exc:  # PlaywrightNotInstalled etc.
                self.browser_login_status = f"Error: {exc}"
            finally:
                self.browser_login_running = False

        threading.Thread(target=worker, daemon=True).start()

    def browser_login_state(self) -> dict:
        return {
            "running": self.browser_login_running,
            "status": self.browser_login_status,
            "logged_in": self.browser_logged_in,
        }

    def refresh_browser_login(self, mobile: bool = False) -> bool:
        """Check the persisted browser session (no visible window)."""
        try:
            with self._browser_lock:
                engine = self._make_browser_engine(mobile)
                self.browser_logged_in = engine.is_logged_in()
        except Exception as exc:
            self.browser_login_status = f"Error: {exc}"
            self.browser_logged_in = False
        return self.browser_logged_in

    # ---- downloading --------------------------------------------------
    def start_download(self, profile_name: str, engine: str = "instaloader",
                       mobile: bool = False) -> DownloadJob:
        job = DownloadJob(job_id=profile_name + "-" + str(int(time.time())),
                          profile=profile_name)
        self.jobs[job.job_id] = job
        if engine == "browser":
            target = self._run_browser_download
            args = (job, mobile)
        else:
            target = self._run_download
            args = (job,)
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        return job

    def _run_browser_download(self, job: DownloadJob, mobile: bool) -> None:
        self._set(job, state="running", message="Starting real browser...")

        def on_progress(done, total, msg):
            fields = {"done": done, "message": msg}
            if total:
                fields["total"] = total
            self._set(job, **fields)

        try:
            with self._browser_lock:
                engine = self._make_browser_engine(mobile)
                saved = engine.download_profile(
                    job.profile,
                    os.path.join(self.download_root, job.profile),
                    require_login=False,
                    on_progress=on_progress,
                )
            if saved == 0:
                self._set(job, state="error",
                          error="No media found. The profile may be private (log in "
                                "via the browser first), empty, or unavailable.")
            else:
                self._set(job, state="done",
                          message=f"Finished. Downloaded {saved} files.")
        except Exception as exc:
            # Covers PlaywrightNotInstalled, BrowserLoginRequired, ValueError, etc.
            self._set(job, state="error", error=str(exc))

    def _set(self, job: DownloadJob, **kwargs) -> None:
        with job.lock:
            for k, v in kwargs.items():
                setattr(job, k, v)

    def _run_download(self, job: DownloadJob) -> None:
        self._set(job, state="running", message="Looking up profile...")
        try:
            profile = Profile.from_username(self.loader.context, job.profile)
        except ProfileNotExistsException:
            self._set(job, state="error", error="That profile does not exist.")
            return
        except LoginRequiredException:
            self._set(job, state="error",
                      error="Login required to view this profile. Please log in.")
            return
        except ConnectionException as exc:
            self._set(job, state="error", error=f"Connection error: {exc}")
            return

        self._set(job, full_name=profile.full_name, is_private=profile.is_private,
                  total=profile.mediacount, message="Starting download...")

        # Private profile guard.
        if profile.is_private and not profile.followed_by_viewer:
            self._set(job, state="error",
                      error="This profile is private and the logged-in account does "
                            "not follow it.")
            return

        try:
            # Profile picture first.
            self.loader.download_profilepic(profile)

            done = 0
            for post in profile.get_posts():
                self.loader.download_post(post, target=profile.username)
                done += 1
                self._set(job, done=done,
                          message=f"Downloaded {done} of {job.total or '?'} posts")

            self._set(job, state="done", done=done,
                      message=f"Finished. Downloaded {done} posts.")
        except (BotDetectedException, TooManyRequestsException) as exc:
            self._set(job, state="error",
                      error=f"Stopped: bot detection / rate limit ({exc}). "
                            f"Wait a while and try again.")
        except PrivateProfileNotFollowedException:
            self._set(job, state="error",
                      error="This profile is private and is not followed by the "
                            "logged-in account.")
        except (QueryReturnedForbiddenException, QueryReturnedBadRequestException) as exc:
            self._set(job, state="error",
                      error=f"Instagram refused the request ({exc}). You may need to "
                            f"log in or wait a while.")
        except LoginRequiredException:
            self._set(job, state="error", error="Login required. Please log in.")
        except ConnectionException as exc:
            self._set(job, state="error", error=f"Connection error: {exc}")

    def get_job(self, job_id: str) -> Optional[DownloadJob]:
        return self.jobs.get(job_id)


# ---- media listing (module-level, stateless) --------------------------
def media_type(filename: str) -> Optional[str]:
    ext = os.path.splitext(filename)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def list_media(profile_dir: str) -> List[dict]:
    """Return a sorted list of media files (images + videos) in a profile folder."""
    if not os.path.isdir(profile_dir):
        return []
    items: List[dict] = []
    for name in sorted(os.listdir(profile_dir)):
        path = os.path.join(profile_dir, name)
        if not os.path.isfile(path):
            continue
        kind = media_type(name)
        if kind is None:
            continue
        items.append({
            "name": name,
            "type": kind,
            "size": os.path.getsize(path),
        })
    return items
