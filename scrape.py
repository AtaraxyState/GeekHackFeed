#!/usr/bin/env python3
"""Scrape a geekhack board into a categorised, scrollable image feed.

Fetches topic listings, picks the best product shot out of each opening post,
classifies each project by type (keycaps / keyboards / switches / ...) and
renders everything into a single self-contained HTML page.

Needs Pillow for local thumbnails (pip install pillow); falls back to
hotlinking the originals without it.

    python scrape.py                 # board 70, first 3 pages
    python scrape.py --pages 10      # go deeper
    python scrape.py --board 132     # a different board
    python scrape.py --open          # open the feed when done
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import html
import http.cookiejar
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone

try:
    from PIL import Image

    HAVE_PILLOW = True
except ImportError:  # thumbnails are optional, the scrape still works
    HAVE_PILLOW = False

BASE = "https://geekhack.org/index.php"
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "template.html")

USER_AGENT = (
    "geekhack-feed/1.0 (personal reading tool; single-user; "
    "contact via geekhack PM)"
)
TOPICS_PER_PAGE = 50
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGES_PER_TOPIC = 8
CDN_DELAY = 0.1
IMAGE_TIMEOUT = 15  # a CDN that has not answered by now is not going to


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------


class Fetcher:
    """Polite HTTP client: one shared session, fixed delay, bounded retries."""

    def __init__(self, delay=1.0, timeout=30, retries=3):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self._last_request = 0.0
        self._last_cdn = 0.0
        self._lock = threading.Lock()
        jar = http.cookiejar.CookieJar()
        # Carrying cookies keeps SMF from stuffing ?PHPSESSID= into every URL.
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar)
        )
        self.opener.addheaders = [
            ("User-Agent", USER_AGENT),
            ("Accept", "text/html,application/xhtml+xml"),
            ("Accept-Encoding", "gzip"),
            ("Accept-Language", "en-US,en;q=0.9"),
        ]

    def _wait(self):
        with self._lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            self._last_request = time.monotonic()

    def _wait_cdn(self):
        """Third-party image hosts are CDNs, so only geekhack gets the full delay."""
        with self._lock:
            elapsed = time.monotonic() - self._last_cdn
            if elapsed < CDN_DELAY:
                time.sleep(CDN_DELAY - elapsed)
            self._last_cdn = time.monotonic()

    def get_binary(self, url):
        """Fetch an image. Image hosts are picky, so knock as a browser."""
        if "geekhack.org" in url:
            self._wait()
        else:
            self._wait_cdn()
        request = urllib.request.Request(
            url, headers={"User-Agent": BROWSER_UA, "Accept": "image/*,*/*"}
        )
        try:
            with urllib.request.urlopen(request, timeout=IMAGE_TIMEOUT) as resp:
                return resp.read(MAX_IMAGE_BYTES)
        except Exception:  # noqa: BLE001 - dead links come in every flavour
            return None

    def get(self, url):
        """Return decoded page text, or None if it could not be fetched."""
        for attempt in range(self.retries):
            self._wait()
            try:
                with self.opener.open(url, timeout=self.timeout) as resp:
                    raw = resp.read(MAX_BODY_BYTES)
                    if resp.headers.get("Content-Encoding") == "gzip":
                        try:
                            raw = gzip.decompress(raw)
                        except OSError:
                            pass
                    return decode(raw)
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429, 503) and attempt < self.retries - 1:
                    backoff = self.delay * (4 ** (attempt + 1))
                    warn(f"HTTP {exc.code} on {url} - backing off {backoff:.0f}s")
                    time.sleep(backoff)
                    continue
                warn(f"HTTP {exc.code} on {url}")
                return None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self.retries - 1:
                    time.sleep(self.delay * (attempt + 2))
                    continue
                warn(f"{type(exc).__name__} on {url}: {exc}")
                return None
        return None


def decode(raw: bytes) -> str:
    """geekhack declares ISO-8859-1 but occasionally carries real UTF-8."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def warn(msg):
    print(f"  ! {msg}", file=sys.stderr)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

ROW_RE = re.compile(
    r'<td class="subject(?P<cls>[^"]*)">(?P<subject>.*?)</td>\s*'
    r'<td class="stats[^"]*">(?P<stats>.*?)</td>\s*'
    r'<td class="lastpost[^"]*">(?P<lastpost>.*?)</td>',
    re.S,
)
TOPIC_LINK_RE = re.compile(r'<a href="[^"]*topic=(\d+)\.0[^"]*"[^>]*>(.*?)</a>', re.S)
STARTER_RE = re.compile(r"Started by\s*<a[^>]*>(.*?)</a>", re.S)
REPLIES_RE = re.compile(r"([\d,]+)\s*Replies")
VIEWS_RE = re.compile(r"([\d,]+)\s*Views")
DATE_RE = re.compile(
    r"\b\w{3},\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4}),\s*(\d{2}):(\d{2}):(\d{2})"
)
FIRST_POST_RE = re.compile(r'<div class="inner" id="msg_(\d+)">', re.S)
POST_WRAPPER_RE = re.compile(r'<div class="post_wrapper">')
OPENED_ON_RE = re.compile(r"<strong>\s*on:\s*</strong>(.*?)&#187;", re.S)
IMG_RE = re.compile(r"<img\b[^>]*>", re.S)
SRC_RE = re.compile(r'\bsrc="([^"]+)"')

MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}


def clean_text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def parse_date(fragment: str):
    """SMF prints 'Wed, 24 June 2026, 00:57:41'. Return ISO 8601 or None."""
    match = DATE_RE.search(fragment)
    if not match:
        return None
    day, month_name, year, hour, minute, second = match.groups()
    month = MONTHS.get(month_name.lower())
    if not month:
        return None
    try:
        dt = datetime(
            int(year), month, int(day), int(hour), int(minute), int(second),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None
    return dt.isoformat()


def parse_board_page(page_html: str):
    """Yield one dict per topic row on a board listing page."""
    topics = []
    for row in ROW_RE.finditer(page_html):
        subject = row.group("subject")
        link = TOPIC_LINK_RE.search(subject)
        if not link:
            continue
        starter = STARTER_RE.search(subject)
        replies = REPLIES_RE.search(row.group("stats"))
        views = VIEWS_RE.search(row.group("stats"))
        topics.append(
            {
                "id": int(link.group(1)),
                "title": clean_text(link.group(2)),
                "author": clean_text(starter.group(1)) if starter else "",
                "replies": int(replies.group(1).replace(",", "")) if replies else 0,
                "views": int(views.group(1).replace(",", "")) if views else 0,
                "last_post": parse_date(row.group("lastpost")),
                "sticky": "sticky" in row.group("cls"),
            }
        )
    return topics


def is_content_image(src: str, tag: str) -> bool:
    """Keep user-posted images; drop theme furniture, avatars and emoji."""
    if "bbc_img" not in tag and "dlattach" not in src:
        return False
    lowered = src.lower()
    if "/themes/" in lowered or "type=avatar" in lowered:
        return False
    if "/images/post/" in lowered or "/smileys/" in lowered:
        return False
    return lowered.startswith("http")


TEXT_SNIPPET_CHARS = 2500


def parse_topic_page(page_html: str):
    """Extract opening-post date, images and body text from a topic page."""
    result = {"created": None, "images": [], "text": ""}

    opened = OPENED_ON_RE.search(page_html)
    if opened:
        result["created"] = parse_date(opened.group(1))

    first = FIRST_POST_RE.search(page_html)
    if not first:
        return result

    start = first.end()
    # The opening post ends where the second post's wrapper begins.
    nxt = POST_WRAPPER_RE.search(page_html, start)
    body = page_html[start : nxt.start() if nxt else len(page_html)]

    # The opening post's prose is the best signal for what a project actually
    # is -- plenty of titles are just a product name ("Rukia", "Enso-E").
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", body, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    result["text"] = re.sub(r"\s+", " ", text).strip()[:TEXT_SNIPPET_CHARS]

    seen = set()
    for tag in IMG_RE.finditer(body):
        src_match = SRC_RE.search(tag.group(0))
        if not src_match:
            continue
        src = html.unescape(src_match.group(1))
        if not is_content_image(src, tag.group(0)) or src in seen:
            continue
        seen.add(src)
        result["images"].append(src)
        if len(result["images"]) >= MAX_IMAGES_PER_TOPIC:
            break
    return result


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

# (category, body_factor, patterns). Ordered most-specific first; ties fall to
# the earlier category. body_factor scales how much the opening post counts:
# parts are listed inside every keyboard thread, so they must win on the title
# alone or not at all.
CATEGORY_RULES = [
    (
        "Deskmats",
        0.8,
        [
            (r"\bdesk\s?mats?\b", 10),
            (r"\bdesk\s?pads?\b", 8),
            (r"\bmouse\s?pads?\b", 8),
        ],
    ),
    (
        "Artisans",
        0.8,
        [
            (r"\bartisans?\b", 10),
            (r"\bresin\b", 6),
            (r"\bsculpts?\b", 5),
            (r"\bcast(ing)?\b", 3),
        ],
    ),
    (
        "Cables",
        0.8,
        [
            (r"\bcables?\b", 10),
            (r"\bcoiled\b", 7),
            (r"\baviator\b", 7),
            (r"\bgx-?16\b", 7),
            (r"\blemo\b", 5),
        ],
    ),
    (
        "Switches",
        0.6,  # every keyboard thread talks about switches it ships with
        [
            (r"\bswitch(es)?\b", 10),
            # "beam spring" names a whole keyboard family, not a switch GB.
            (r"(?<!beam\s)\bsprings?\b", 7),
            (r"\bswitch\s?films?\b", 7),
            (r"\bclicky\b", 6),
            (r"\bstems?\b", 5),
            (r"\blinear\b", 5),
            (r"\btactile\b", 5),
            (r"\blubed?\b", 4),
        ],
    ),
    (
        "Keycaps",
        1.0,
        [
            (r"\bkey\s?caps?\b", 9),
            (r"\bkey\s?sets?\b", 9),
            # Profiles and keycap-only vendor prefixes are unambiguous markers.
            (r"\bgmk\b", 9),
            (r"\bcyl\b", 8),
            (r"\bmtnu\b", 8),
            (r"\bmt3\b", 8),
            (r"\bepbt\b", 8),
            (r"\benjoypbt\b", 8),
            (r"\bnicepbt\b", 8),
            (r"\bdss\b", 8),
            (r"\bdcs\b", 7),
            (r"\bkat\b", 7),
            (r"\bjtk\b", 7),
            (r"\bdsa\b", 7),
            (r"\bxda\b", 7),
            (r"\bkkb\b", 7),
            (r"\bswg\b", 7),
            (r"\bslk\b", 7),
            (r"\bpbs\b", 6),
            (r"\bkeykobo\b", 7),
            (r"\bmw\b", 6),
            (r"\bsa\b", 6),
            (r"\bmono\s?kit\b", 8),
            (r"\b(?:fix|extension|novelty|base|add-?on|alphas?|spacebar)\s+kits?\b", 7),
            (r"\bwob\b", 6),
            (r"\bcherry\s+profile\b", 6),
            (r"\bdye-?sub\b", 6),
            (r"\bdouble-?shot\b", 6),
            (r"\bnovelt(y|ies)\b", 5),
            (r"\blegends?\b", 4),
            (r"\bcolou?rway\b", 4),
            (r"\bpbt\b", 4),
            (r"\babs\b", 3),
        ],
    ),
    (
        "Keyboards",
        1.0,
        [
            (r"\bkey\s?boards?\b", 9),
            (r"\bbeam\s?spring\b", 9),
            (r"\btkl\b", 8),
            (r"\b(?:40|60|61|65|68|70|75|80|87|96|98|100|104)\s?%", 8),
            # Board names bake the size in without a percent sign: Shy60, Navi60.
            (r"[a-z](?:40|60|61|65|68|70|75|80|87|96|98|100|104)\b", 6),
            (r"\b(?:40|60|65|75|80)s\b", 5),
            (r"\bnum\s?pad\b", 7),
            (r"\bmacro\s?pad\b", 7),
            # ZoomPad, Keypad, NumPad -- Deskmats outscores this for deskpads.
            (r"\b\w+pad\b", 5),
            (r"\balice\b", 6),
            (r"\bhhkb\b", 6),
            (r"\bkeeb\b", 6),
            (r"\bhot-?swap\b", 6),
            (r"\bgasket\b", 6),
            (r"\bpcb\b", 6),
            (r"\bergo(dox|nomic)?\b", 5),
            (r"\bsouthpaw\b", 5),
            (r"\blow\s?profile\b", 5),
            (r"\bpoly\s?carb(onate)?\b", 5),
            (r"\balumin(i)?um\b", 4),
            (r"\bsplit\b", 4),
            (r"\bfull-?\s?size\b", 4),
            (r"\blayouts?\b", 4),
            (r"\bmounts?\b", 3),
            (r"\bcases?\b", 3),
            (r"\bkits?\b", 2),
        ],
    ),
    (
        "Parts & Accessories",
        0.0,  # title-only: these words appear in every keyboard spec sheet
        [
            (r"\bstabili[sz]ers?\b", 9),
            (r"\bstabs?\b", 8),
            (r"\bdomes?\b", 8),
            (r"\bplates?\b", 6),
            (r"\bfoam\b", 6),
            (r"\bweights?\b", 5),
            (r"\bbadges?\b", 5),
            (r"\bwrist\s?rests?\b", 8),
            (r"\bcarry(ing)?\s?cases?\b", 6),
            (r"\bo-?rings?\b", 6),
            (r"\bscrew-?ins?\b", 5),
            (r"\btools?\b", 3),
        ],
    ),
]

COMPILED_RULES = [
    (name, factor, [(re.compile(pat, re.I), weight) for pat, weight in patterns])
    for name, factor, patterns in CATEGORY_RULES
]

# Organisers tag threads with [GB], (GB) and the full-width 【GB】 alike.
OPEN_TAG = r"\[\(【（"
CLOSE_TAG = r"\]\)】）"
TAG_RE = re.compile(rf"[{OPEN_TAG}]\s*([^{CLOSE_TAG}]{{1,40}}?)\s*[{CLOSE_TAG}]")

# Where a project has got to. Read from the whole title, since organisers
# append the status after the bracket: "[GB] GMK Foundation - Shipping".
PROGRESS_RULES = [
    ("Shipping", r"\bship(ping|ped)\b|\bfulfill(ed|ing|ment)\b|\bdelivering\b"),
    ("Complete", r"\bcomplete[d]?\b|\bclosed\b|\bended\b|\bcancell?ed\b|\bconcluded\b|\bgb\s+over\b"),
    ("In Production", r"\bin\s?production\b|\bmoq\s+reached\b"),
    ("Live", r"\blive\b|\bnow\s+open\b|\bextended\b|\bin-?\s?stock\b"),
]

# What kind of thread it is. The bracket prefix is authoritative here.
TYPE_RULES = [
    ("Interest Check", r"\bic\b|\binterest\s?check\b"),
    ("Group Buy", r"\bgb\b|\bgroup\s?buy\b|\bpre-?\s?order\b|\bextras?\b"),
]

COMPILED_PROGRESS = [(name, re.compile(pat, re.I)) for name, pat in PROGRESS_RULES]
COMPILED_TYPES = [(name, re.compile(pat, re.I)) for name, pat in TYPE_RULES]


TITLE_WEIGHT = 3.0
BODY_WEIGHT = 0.25
BODY_MAX_HITS = 4


def classify(title: str, body: str = "") -> str:
    """Score every category over the title, then the opening post as backup.

    A title match counts for much more than a body match, but body evidence is
    what rescues the many threads named only after the product.
    """
    scores = []
    for name, factor, patterns in COMPILED_RULES:
        score = 0.0
        for rx, weight in patterns:
            if rx.search(title):
                score += weight * TITLE_WEIGHT
            elif body and factor:
                hits = min(len(rx.findall(body)), BODY_MAX_HITS)
                score += weight * hits * BODY_WEIGHT * factor
        scores.append((score, name))

    best_score = max(score for score, _ in scores)
    if best_score < 1.0:
        return "Other"
    for score, name in scores:  # rules are ordered, so the first max wins
        if score == best_score:
            return name
    return "Other"


def detect_stage(title: str) -> str:
    """Where the project stands: how far along beats what it is called.

    "[GB] GMK Foundation - Shipping to Vendors" is more useful filed under
    Shipping than under the Group Buy every thread on the board shares.
    """
    for name, rx in COMPILED_PROGRESS:
        if rx.search(title):
            return name

    tags = " ".join(TAG_RE.findall(title))
    for name, rx in COMPILED_TYPES:
        if rx.search(tags):
            return name
    for name, rx in COMPILED_TYPES:
        if rx.search(title):
            return name
    return "Unknown"


def clean_title(title: str) -> str:
    """Strip leading bracket tags so the card shows the project name itself."""
    stripped = title
    while True:
        new = re.sub(
            rf"^\s*[{OPEN_TAG}][^{CLOSE_TAG}]{{0,40}}[{CLOSE_TAG}]\s*[-:|]?\s*",
            "",
            stripped,
        )
        if new == stripped:
            break
        stripped = new
    stripped = re.sub(
        rf"\s*[{OPEN_TAG}][^{CLOSE_TAG}]{{0,40}}[{CLOSE_TAG}]\s*$", "", stripped
    ).strip()
    return stripped or title


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------


def topic_url(topic_id: int) -> str:
    return f"{BASE}?topic={topic_id}.0"


# Bump whenever parse_topic_page starts storing something new, so old caches
# do not silently starve the classifier of fields it now expects.
CACHE_VERSION = 3


def load_cache(path):
    if not os.path.exists(path):
        return {"_version": CACHE_VERSION}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, json.JSONDecodeError):
        warn(f"could not read cache at {path}; starting fresh")
        return {"_version": CACHE_VERSION}
    if cache.get("_version") != CACHE_VERSION:
        print("  cache format changed - re-fetching topic details")
        return {"_version": CACHE_VERSION}
    return cache


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def save_cache(path, cache):
    save_json(path, cache)


# --------------------------------------------------------------------------
# cover images
# --------------------------------------------------------------------------

# Opening posts lead with vendor banners and section dividers far more often
# than with the product itself, so the filename is a useful first filter.
BANNER_HINT_RE = re.compile(
    r"banner|logo|header|footer|divider|spacer|signature|separator|"
    r"\bsig\b|\bbar\b|\bline\d*\b|watermark|button",
    re.I,
)
# Only about a quarter of candidates have a descriptive filename -- the rest
# are opaque imgur hashes -- so this is a bonus signal, never the main one.
PRODUCT_HINT_RE = re.compile(
    r"\bkits?\b|base|novelt|alphas?|spacebar|numpad|render|board|keycap|"
    r"\bset\b|colou?rs?|extension|mono|angle|front|top|side|shot",
    re.I,
)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
IMGUR_RE = re.compile(r"^(https?://i\.imgur\.com/[A-Za-z0-9]{5,10})(\.[a-z]{3,4})$", re.I)

MAX_CANDIDATES = 5
MIN_COVER_W, MIN_COVER_H = 220, 140
THUMB_MAX = 720
IMAGE_WORKERS = 4
TOPIC_BYTE_BUDGET = 8 * 1024 * 1024  # stop trying candidates past this
POSITION_DECAY = 0.72  # the hero shot usually leads the post


def thumbnail_source(src: str) -> str:
    """imgur serves a 640px version if you suffix the id with 'l'.

    Covers average north of a megabyte otherwise, which is what makes the
    feed crawl. Costs imgur far less too.
    """
    match = IMGUR_RE.match(src)
    return f"{match.group(1)}l{match.group(2)}" if match else src


def aspect_weight(aspect: float) -> float:
    """Keyboards and keycap renders are landscape. Square is album art.

    Opening posts are full of inspiration imagery, cover scans and logos, and
    those are overwhelmingly square. The product shot almost never is.
    """
    if aspect > 5.0:
        return 0.20  # a strip across the top of the post
    if aspect > 3.2:
        return 1.00  # wide kit diagram
    if aspect >= 1.25:
        return 1.30  # the shape of a keyboard photo or keyset render
    if aspect >= 1.18:
        return 1.00
    if aspect >= 0.85:
        return 0.45  # square: album covers, logos, avatars
    return 0.60  # portrait


def score_image(img, src: str, position: int):
    """Rate a candidate as the product shot, or None to reject it."""
    width, height = img.size
    if width < MIN_COVER_W or height < MIN_COVER_H:
        return None

    score = (width * height) ** 0.5
    score *= aspect_weight(width / height)
    score *= POSITION_DECAY**position

    name = os.path.basename(urllib.parse.urlparse(src).path)
    if BANNER_HINT_RE.search(name):
        score *= 0.12
    elif PRODUCT_HINT_RE.search(name):
        score *= 2.2
    return score


def save_thumbnail(img, dest: str):
    img = img.convert("RGB")
    img.thumbnail((THUMB_MAX, THUMB_MAX), Image.LANCZOS)
    img.save(dest, "JPEG", quality=82, optimize=True)


def pick_cover(fetcher, project, out_dir):
    """Walk the opening post's images and keep the best product shot."""
    dest = os.path.join(out_dir, f"{project['id']}.jpg")
    rel = f"images/{project['id']}.jpg"
    if os.path.exists(dest):
        return rel, True

    best = None  # (score, PIL image)
    spent = 0
    for position, src in enumerate(project["images"][:MAX_CANDIDATES]):
        data = fetcher.get_binary(thumbnail_source(src))
        if not data or len(data) < 1500:  # dead imgur links come back tiny
            continue
        spent += len(data)
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception:  # noqa: BLE001 - hosts serve HTML error pages as images
            continue

        score = score_image(img, src, position)
        if score is not None and (best is None or score > best[0]):
            best = (score, img)
        if spent > TOPIC_BYTE_BUDGET:
            break

    if best is None:
        return None, False
    try:
        save_thumbnail(best[1], dest)
    except Exception as exc:  # noqa: BLE001
        warn(f"could not write thumbnail for topic {project['id']}: {exc}")
        return None, False
    return rel, False


def cover_index_path(board):
    return os.path.join(HERE, f"covers-board{board}.json")


def build_covers(fetcher, projects, out_dir, board, force=False):
    """Fetch and thumbnail every cover, a few hosts at a time.

    Decisions are remembered, including the failures. A topic whose images are
    all dead would otherwise re-attempt every candidate on every refresh, and
    at a 5 minute cadence those timeouts add up to a permanently busy server.
    """
    if not HAVE_PILLOW:
        warn("Pillow not installed - hotlinking full-size images instead")
        warn("install it with: pip install pillow")
        for project in projects:
            project["local_image"] = None
        return

    os.makedirs(out_dir, exist_ok=True)
    index = {} if force else load_json(cover_index_path(board), {})

    todo = []
    for project in projects:
        key = str(project["id"])
        if key in index:
            rel = index[key]
            # Trust the index only while the file it points at is still there.
            if rel is None or os.path.exists(os.path.join(HERE, rel)):
                project["local_image"] = rel
                continue
        project["local_image"] = None
        if project["images"]:
            todo.append(project)

    if not todo:
        have = sum(1 for p in projects if p["local_image"])
        print(f"  {have} covers, all from cache")
        return

    done = downloaded = reused = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as pool:
        futures = {pool.submit(pick_cover, fetcher, p, out_dir): p for p in todo}
        for future in concurrent.futures.as_completed(futures):
            project = futures[future]
            done += 1
            try:
                rel, cached = future.result()
            except Exception as exc:  # noqa: BLE001
                warn(f"cover failed for topic {project['id']}: {exc}")
                rel, cached = None, False
            project["local_image"] = rel
            index[str(project["id"])] = rel
            if rel and cached:
                reused += 1
            elif rel:
                downloaded += 1
            if done % 25 == 0:
                print(f"  {done}/{len(todo)} covers")

    save_json(cover_index_path(board), index)
    print(
        f"  {downloaded} downloaded, {reused} already on disk, "
        f"{done - downloaded - reused} with nothing usable"
    )


def scrape(args):
    fetcher = Fetcher(delay=args.delay)
    cache_path = os.path.join(HERE, f"cache-board{args.board}.json")
    cache = load_cache(cache_path)

    listings = []
    for page in range(args.pages):
        offset = page * TOPICS_PER_PAGE
        url = f"{BASE}?board={args.board}.{offset}"
        print(f"[listing] page {page + 1}/{args.pages}  {url}")
        page_html = fetcher.get(url)
        if not page_html:
            warn("listing page failed; stopping pagination here")
            break
        rows = parse_board_page(page_html)
        if not rows:
            print("  no topics found - reached the end of the board")
            break
        listings.extend(rows)

    # Announcements and rules threads are not projects.
    projects = [t for t in listings if not (args.skip_sticky and t["sticky"])]
    seen_ids = set()
    unique = []
    for topic in projects:
        if topic["id"] not in seen_ids:
            seen_ids.add(topic["id"])
            unique.append(topic)
    projects = unique
    print(f"\n{len(projects)} topics to process ({len(listings) - len(projects)} skipped)")

    fetched = 0
    for index, project in enumerate(projects, start=1):
        key = str(project["id"])
        cached = cache.get(key)
        if cached and not args.refresh:
            project.update(cached)
            continue
        print(f"[{index}/{len(projects)}] {project['title'][:70]}")
        page_html = fetcher.get(topic_url(project["id"]))
        detail = (
            parse_topic_page(page_html)
            if page_html
            else {"created": None, "images": [], "text": ""}
        )
        cache[key] = detail
        project.update(detail)
        fetched += 1
        if fetched % 20 == 0:
            save_cache(cache_path, cache)

    save_cache(cache_path, cache)
    print(f"\nfetched {fetched} topic page(s), {len(projects) - fetched} from cache")

    for project in projects:
        text = project.pop("text", "")
        project["category"] = classify(project["title"], text)
        project["stage"] = detect_stage(project["title"])
        project["name"] = clean_title(project["title"])
        project["url"] = topic_url(project["id"])
        project["local_image"] = None
        # Lets a merged feed tell forum threads from vendor listings.
        project["source"] = "geekhack"
        # Keep a short blurb for the card and the search index; the full 2.5k
        # snippet stays in the cache so the page does not balloon.
        project["excerpt"] = text[:220].strip()

    if args.hotlink:
        for project in projects:
            project["local_image"] = None
    else:
        print("\npicking cover images...")
        build_covers(
            fetcher,
            projects,
            os.path.join(HERE, "images"),
            args.board,
            force=args.refresh,
        )

    return projects


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def render(projects, board, out_path):
    with open(TEMPLATE, "r", encoding="utf-8") as fh:
        template = fh.read()

    payload = {
        "board": board,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "projects": projects,
    }
    # </script> inside the JSON would close the tag early.
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    page = template.replace("/*__DATA__*/null", blob)

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return out_path


def summarise(projects, hotlink=False):
    counts = {}
    for project in projects:
        counts[project["category"]] = counts.get(project["category"], 0) + 1
    key = "images" if hotlink else "local_image"
    with_cover = sum(1 for p in projects if p.get(key))
    print("\nby category:")
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {name}")
    print(f"\n{with_cover}/{len(projects)} projects have a usable cover")


def main():
    parser = argparse.ArgumentParser(
        description="Scrape a geekhack board into a categorised image feed."
    )
    parser.add_argument("--board", type=int, default=70, help="board id (default: 70)")
    parser.add_argument(
        "--pages", type=int, default=3, help="listing pages, 50 topics each (default: 3)"
    )
    parser.add_argument(
        "--delay", type=float, default=1.0, help="seconds between requests (default: 1.0)"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="re-fetch topics already in the cache"
    )
    parser.add_argument(
        "--hotlink",
        action="store_true",
        help="link covers at their original host instead of caching thumbnails",
    )
    parser.add_argument(
        "--keep-sticky",
        dest="skip_sticky",
        action="store_false",
        help="include pinned announcement threads",
    )
    parser.add_argument("--out", default=None, help="output HTML path")
    parser.add_argument("--open", action="store_true", help="open the feed when done")
    parser.set_defaults(skip_sticky=True)
    args = parser.parse_args()

    if args.delay < 0.5:
        warn("delay below 0.5s is impolite to a volunteer-run forum; raising to 0.5")
        args.delay = 0.5

    projects = scrape(args)
    if not projects:
        print("nothing scraped - is the board id right?")
        return 1

    out_path = args.out or os.path.join(HERE, f"feed-board{args.board}.html")
    render(projects, args.board, out_path)
    summarise(projects, args.hotlink)
    print(f"\nwrote {out_path}")

    if args.open:
        webbrowser.open(f"file:///{out_path.replace(os.sep, '/')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
