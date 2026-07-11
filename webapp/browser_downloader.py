"""
Real browser download engine (Playwright).

Instead of talking to Instagram's private API with a bare HTTP client (which gets
429'd quickly), this drives a *real* Chromium browser. Instagram's own JavaScript
runs, makes the API calls with legitimate cookies/headers, and we simply listen to
the JSON responses that fly by and collect media URLs from them. Because the
traffic is indistinguishable from a person browsing, bot detection is much lower.

Two emulation profiles are supported:
  * "desktop" - a normal desktop Chromium (Windows Chrome UA).
  * "mobile"  - iPhone device emulation (mobile Safari UA, touch, mobile viewport).

Everything here manages its own Playwright lifecycle inside a single thread, so it
can be driven from a background worker thread in the web app. Playwright's sync API
is thread-affine, so never share these objects across threads.
"""

import json
import os
import re
import time
from typing import Callable, Dict, List, Optional

# Desktop Chrome on Windows.
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
VIDEO_EXTS = {".mp4", ".mov", ".webm"}

# Instagram response URLs that carry post/media data.
MEDIA_API_PATTERNS = (
    "web_profile_info",
    "graphql/query",
    "api/v1/feed/user",
    "/feed/user/",
    "api/v1/users/",
)


class PlaywrightNotInstalled(Exception):
    pass


class BrowserLoginRequired(Exception):
    pass


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as exc:
        raise PlaywrightNotInstalled(
            "Playwright is not installed. Run:\n"
            "    pip install playwright\n"
            "    python -m playwright install chromium"
        ) from exc


# ----------------------------------------------------------------------------
# Media extraction from arbitrary Instagram JSON
# ----------------------------------------------------------------------------
def _best_image_url(node: dict) -> Optional[str]:
    # New API: image_versions2.candidates[].url (first = highest res)
    iv = node.get("image_versions2")
    if isinstance(iv, dict):
        cands = iv.get("candidates") or []
        if cands and isinstance(cands, list):
            return cands[0].get("url")
    # GraphQL: display_url / display_resources
    if node.get("display_url"):
        return node["display_url"]
    dr = node.get("display_resources")
    if isinstance(dr, list) and dr:
        return dr[-1].get("src")
    return None


def _best_video_url(node: dict) -> Optional[str]:
    vv = node.get("video_versions")
    if isinstance(vv, list) and vv:
        return vv[0].get("url")
    if node.get("video_url"):
        return node["video_url"]
    return None


def _node_id(node: dict) -> str:
    for key in ("pk", "id", "code", "shortcode"):
        if node.get(key):
            return str(node[key])
    # fall back to a hash of the media url
    return str(abs(hash(json.dumps(node, sort_keys=True, default=str))))[:16]


def extract_media_from_json(data) -> List[dict]:
    """
    Recursively walk any Instagram JSON and pull out media items.

    Returns a list of {id, type, url} dicts. Handles single images, videos,
    and carousels (sidecar / carousel_media), across both the GraphQL and the
    private-API ("v1") response shapes.
    """
    found: List[dict] = []
    seen: set = set()

    def add(node: dict):
        is_video = bool(node.get("is_video")) or node.get("media_type") == 2 \
            or "video_versions" in node or "video_url" in node
        url = _best_video_url(node) if is_video else _best_image_url(node)
        if not url:
            # A node may be an image even if is_video was falsely set; try both.
            url = _best_image_url(node) or _best_video_url(node)
            if not url:
                return
            is_video = url.split("?")[0].lower().endswith((".mp4", ".mov", ".webm"))
        key = _node_id(node) + ("|v" if is_video else "|i")
        if key in seen:
            return
        seen.add(key)
        found.append({
            "id": _node_id(node),
            "type": "video" if is_video else "image",
            "url": url,
        })

    def walk(obj):
        if isinstance(obj, dict):
            # Carousel children (GraphQL)
            sidecar = obj.get("edge_sidecar_to_children")
            if isinstance(sidecar, dict):
                for edge in sidecar.get("edges", []):
                    node = edge.get("node")
                    if isinstance(node, dict):
                        add(node)
            # Carousel children (private API)
            carousel = obj.get("carousel_media")
            if isinstance(carousel, list):
                for child in carousel:
                    if isinstance(child, dict):
                        add(child)
            # A media node itself?
            if any(k in obj for k in ("display_url", "image_versions2",
                                      "video_url", "video_versions")):
                if not obj.get("edge_sidecar_to_children") and not obj.get("carousel_media"):
                    add(obj)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return found


def _ext_for(url: str, kind: str) -> str:
    path = url.split("?")[0]
    _, ext = os.path.splitext(path)
    ext = ext.lower()
    if kind == "video" and ext not in VIDEO_EXTS:
        return ".mp4"
    if kind == "image" and ext not in IMAGE_EXTS:
        return ".jpg"
    return ext or (".mp4" if kind == "video" else ".jpg")


# ----------------------------------------------------------------------------
# Browser engine
# ----------------------------------------------------------------------------
class BrowserEngine:
    """
    Drives a persistent Chromium context. The user-data dir keeps the login
    session between runs, so you only log in once.
    """

    def __init__(self, user_data_dir: str, mobile: bool = False):
        _require_playwright()
        self.user_data_dir = user_data_dir
        self.mobile = mobile
        os.makedirs(user_data_dir, exist_ok=True)

    # -- context helpers ----------------------------------------------------
    def _launch(self, pw, headless: bool):
        """Launch a persistent context with desktop or mobile emulation."""
        kwargs = dict(
            user_data_dir=self.user_data_dir,
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        if self.mobile:
            iphone = dict(pw.devices["iPhone 13"])  # copy so we can edit it
            # 'default_browser_type' is part of the device descriptor but is not a
            # valid launch_persistent_context() argument, so drop it.
            iphone.pop("default_browser_type", None)
            kwargs.update(iphone)  # sets UA, viewport, touch, device scale, etc.
        else:
            kwargs.update(user_agent=DESKTOP_UA, viewport={"width": 1366, "height": 900})
        context = pw.chromium.launch_persistent_context(**kwargs)
        # Hide the most obvious webdriver signal.
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        return context

    def _is_logged_in(self, context) -> bool:
        for cookie in context.cookies():
            if cookie.get("name") == "sessionid" and cookie.get("value"):
                return True
        return False

    # -- public: interactive login -----------------------------------------
    def open_login(self, wait_seconds: int = 300,
                   on_status: Optional[Callable[[str], None]] = None) -> bool:
        """
        Open a *visible* browser at the Instagram login page and wait until the
        user has logged in (a `sessionid` cookie appears) or until timeout.

        Returns True if login succeeded. Session persists in user_data_dir.
        """
        from playwright.sync_api import sync_playwright

        def status(msg):
            if on_status:
                on_status(msg)

        with sync_playwright() as pw:
            context = self._launch(pw, headless=False)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                if self._is_logged_in(context):
                    status("Already logged in.")
                    return True
                status("Opening Instagram login in a real browser window...")
                page.goto("https://www.instagram.com/accounts/login/",
                          wait_until="domcontentloaded", timeout=60000)
                status("Please log in inside the browser window (handles 2FA too).")
                deadline = time.time() + wait_seconds
                while time.time() < deadline:
                    if self._is_logged_in(context):
                        status("Login detected. You can close the window.")
                        time.sleep(1.5)
                        return True
                    time.sleep(1.5)
                status("Timed out waiting for login.")
                return False
            finally:
                context.close()

    def is_logged_in(self) -> bool:
        """Check persisted session without showing a window."""
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            context = self._launch(pw, headless=True)
            try:
                return self._is_logged_in(context)
            finally:
                context.close()

    # -- public: download a profile ----------------------------------------
    def download_profile(
        self,
        username: str,
        dest_dir: str,
        require_login: bool = False,
        max_scrolls: int = 200,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> int:
        """
        Scrape and download all media for `username` into `dest_dir`.

        Works by navigating to the profile in a real browser and collecting media
        URLs from the JSON responses Instagram's own frontend fetches, while
        scrolling to load every post. Returns the number of files saved.
        """
        from playwright.sync_api import sync_playwright

        os.makedirs(dest_dir, exist_ok=True)

        def progress(done, total, msg):
            if on_progress:
                on_progress(done, total, msg)

        media: Dict[str, dict] = {}

        with sync_playwright() as pw:
            context = self._launch(pw, headless=True)
            try:
                if require_login and not self._is_logged_in(context):
                    raise BrowserLoginRequired(
                        "This profile needs a logged-in session. Use 'Open browser "
                        "to log in' first."
                    )

                page = context.pages[0] if context.pages else context.new_page()

                def on_response(response):
                    url = response.url
                    if not any(p in url for p in MEDIA_API_PATTERNS):
                        return
                    ctype = response.headers.get("content-type", "")
                    if "application/json" not in ctype:
                        return
                    try:
                        data = response.json()
                    except Exception:
                        return
                    for item in extract_media_from_json(data):
                        media.setdefault(item["id"] + item["type"], item)

                page.on("response", on_response)

                progress(0, 0, "Opening profile...")
                page.goto(f"https://www.instagram.com/{username}/",
                          wait_until="domcontentloaded", timeout=60000)
                time.sleep(3)

                # Detect obvious "page not found" / private states.
                content = (page.content() or "").lower()
                if "sorry, this page isn't available" in content:
                    raise ValueError("Profile not found or unavailable.")

                # Scroll to load all posts. Stop when height stops growing.
                last_count = -1
                stagnant = 0
                for i in range(max_scrolls):
                    page.mouse.wheel(0, 20000)
                    time.sleep(1.5 + (i % 3) * 0.4)  # small jitter
                    count = len(media)
                    progress(count, 0, f"Collecting media... {count} found")
                    if count == last_count:
                        stagnant += 1
                        if stagnant >= 4:
                            break
                    else:
                        stagnant = 0
                        last_count = count

                items = list(media.values())
                total = len(items)
                if total == 0:
                    progress(0, 0, "No media found (profile may be private or empty).")
                    return 0

                # Download each media file using the browser's request context so
                # cookies/headers match exactly.
                saved = 0
                req = context.request
                for idx, item in enumerate(items, start=1):
                    ext = _ext_for(item["url"], item["type"])
                    fname = f"{idx:04d}_{item['id']}{ext}"
                    fpath = os.path.join(dest_dir, fname)
                    try:
                        resp = req.get(item["url"], timeout=60000)
                        if resp.ok:
                            with open(fpath, "wb") as f:
                                f.write(resp.body())
                            saved += 1
                    except Exception:
                        pass
                    progress(saved, total, f"Downloaded {saved}/{total}")
                    time.sleep(0.2)

                progress(saved, total, f"Finished. Saved {saved} files.")
                return saved
            finally:
                context.close()
