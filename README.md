# geekhack feed

Turns a geekhack board into a single scrollable feed: every project as a card
with its product shot, name and a one-line blurb, filterable by type (keycaps /
keyboards / switches / artisans / cables / deskmats / parts).

Three pieces:

| | |
|---|---|
| `scrape.py` | Scrapes a board and writes a self-contained HTML page |
| `serve.py` | Keeps that up to date on a timer and serves it over HTTP |
| `android/` | Phone app that points at the server |

Python 3.8+. Needs [Pillow](https://pypi.org/project/pillow/) for cover
thumbnails (`pip install pillow`); everything else is standard library.

## One-off page

```bash
python scrape.py --pages 3 --open
```

Reads the first 3 listing pages of board 70 (Group Buys and Preorders), ~50
threads each, and writes `feed-board70.html`. Open it in any browser.

| Flag | Default | What it does |
|---|---|---|
| `--board N` | `70` | Which board to scrape |
| `--pages N` | `3` | Listing pages, 50 threads each |
| `--delay S` | `1.0` | Seconds between requests (floor of 0.5) |
| `--refresh` | off | Re-fetch threads and covers already cached |
| `--hotlink` | off | Link covers at their original host, skip thumbnailing |
| `--keep-sticky` | off | Include pinned announcement threads |
| `--out PATH` | — | Where to write the HTML |
| `--open` | off | Open the feed when finished |

The whole board is roughly 60 pages. `--pages 60` takes about an hour at the
default delay; results are cached, so the next run only fetches what is new.

## Server

```bash
python serve.py --interval 5
```

Binds port 8765 on every interface, then scrapes. It starts listening
*before* the first scrape, so the app never gets a refused connection while it
is warming up.

| Route | |
|---|---|
| `/` | the feed page |
| `/api/feed.json` | every project as JSON |
| `/api/status` | last refresh, whether it is busy, error if any |
| `/api/refresh` | force a rescrape now (what the app's menu calls) |
| `/images/<id>.jpg` | cached cover thumbnails |

Every `--interval` minutes it checks the board's **RSS feed** (about 5 KB) and
only runs a real scrape when the hash of that changed. A 5 minute cadence
therefore costs geekhack ~5 KB a poll rather than three full listing pages, and
a warm refresh takes about 5 seconds.

## Android app

`android/geekhack-feed.apk` is a thin client: it renders whatever the server is
currently serving, so improving the feed only means restarting the server, not
rebuilding the app.

1. Run `serve.py` on this machine and note the address it prints.
2. Copy the APK to the phone and install it (needs "install from unknown
   sources" for whatever app you copied it with).
3. First launch asks for the server address — `192.168.x.x:8765`.

Menu: **Refresh** reloads the page, **Rescan geekhack now** calls
`/api/refresh` so you do not have to wait out the timer, **Change server**
re-prompts for the address. Back navigates the WebView; thread links open in
the real browser.

Rebuild it with:

```powershell
cd android; .\build.ps1
```

That drives `aapt2`, `javac`, `d8`, `zipalign` and `apksigner` directly — no
Gradle, no Android Studio, nothing downloaded. It finds the SDK and JDK that
ship with Unity, or takes `-Sdk` / `-Jdk`. Add `-Install` to push it over adb.
The app is plain Java with no AndroidX, which is what keeps the build to a
handful of SDK tool invocations.

Note the APK is signed with a local debug key (`android/debug.keystore`,
generated on first build). That is fine for sideloading onto your own phone; it
is not something you could publish.

## How it works

1. **Listing pages** (`?board=70.<offset>`) give title, author, replies, views
   and last-post time for 50 threads at a time.
2. **Each thread** (`?topic=<id>.0`) is fetched once for its opening post: the
   images (`class="bbc_img"`, so theme furniture and avatars are skipped) and
   the first 2500 characters of text.
3. **Classification** scores keyword rules against the title, then the opening
   post as backup. A title hit counts 3×; body hits count a quarter each.
   That matters because a lot of threads are named only after the product —
   "Rukia", "Enso-E", "Canoe" say nothing, but their opening posts do.

   Parts & Accessories scores on the title *only* (`body_factor = 0.0`), since
   every keyboard thread lists plates, foam and weights in its spec sheet and
   would otherwise be filed as a parts GB. Switches are damped to `0.6` for
   the same reason.
4. **Stage** is read from the title, progress first: a thread tagged `[GB]` but
   titled "… - Shipping to Vendors" is more useful under Shipping than under
   the Group Buy label that 80% of the board shares.

## Picking the cover

The first image in an opening post is usually a vendor banner, and the next few
are often mood-board material — GMK Distortion opens with a banner followed by
three punk album covers before the actual keycap render. Filenames are no help:
73% of them are opaque imgur hashes.

So candidates are scored on geometry and position instead:

- **position** — the hero shot usually leads, so weight decays `0.72^n`
- **shape** — landscape (1.25–3.2) is what a keyboard photo or keyset render
  looks like, so it scores highest; near-square scores 0.45 because that is
  album art, logos and avatars; wider than 5:1 is a banner strip
- **size** — `sqrt(area)`, so bigger wins without dominating
- **filename** — ×2.2 for `kit`/`render`/`base`, ×0.12 for `banner`/`logo`,
  when the filename says anything at all

The winner is downsized to 720px and written to `images/<id>.jpg`. imgur gets
asked for its `l` thumbnail rather than the original, which is the difference
between ~15 KB and ~1.2 MB per cover. Across 142 threads that took the image
set from 156 MB of hotlinks to 5.5 MB on disk, which is also why covers now
show up rather than timing out.

## Caches

- `cache-board70.json` — per-thread details. `CACHE_VERSION` in `scrape.py`
  invalidates it automatically when the scraper starts storing a new field.
- `covers-board70.json` — which image won for each thread, *including* the
  ones where nothing was usable. Without that record a thread whose images are
  all dead retries every candidate on every refresh, and those timeouts are
  enough to keep the server permanently busy at a 5 minute interval.

Delete either, or pass `--refresh`, to redo that work.

## Tuning the categories

`CATEGORY_RULES` in [scrape.py](scrape.py) is a list of
`(category, body_factor, [(regex, weight), ...])`, ordered most-specific first
so ties fall to the earlier entry. Adding a vendor prefix is one line —
keycap-only makers like `KKB`, `SWG` and `SLK` are already in there.

## Politeness

geekhack's `robots.txt` allows everything and declares no crawl delay, but it
is a volunteer-run forum with no CDN in front of it, so every request hits
their origin directly. The scraper therefore:

- sends one request at a time with a 1s gap (floor of 0.5s)
- reuses a cookie session, so SMF stops rewriting URLs with `?PHPSESSID=`
- requests gzip
- caches thread details and cover choices so re-runs cost almost nothing
- polls the 5 KB RSS feed rather than listing pages, and only scrapes for real
  when it changed
- identifies itself in the User-Agent

Put a real contact in `USER_AGENT` if you run this regularly.

Third-party image hosts are CDNs rather than a volunteer forum, so covers are
fetched four at a time with only a 0.1s gap — and asked for thumbnails where
the host offers them.
