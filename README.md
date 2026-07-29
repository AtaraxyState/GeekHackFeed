# geekhack feed

Turns a geekhack board into a single scrollable feed: every project as a card
with its product shot, name and a one-line blurb, filterable by type (keycaps /
keyboards / switches / artisans / cables / deskmats / parts).

Four pieces:

| | |
|---|---|
| `scrape.py` | Scrapes a geekhack board and writes a self-contained HTML page |
| `vendors.py` | Pulls vendor storefronts into the same format |
| `serve.py` | Keeps it all up to date on a timer and serves it over HTTP |
| `android/` | Phone app that points at the server, and updates itself |

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

## Vendor storefronts

```bash
python vendors.py --open
python vendors.py --only omnitype,meletrix
```

Every supported vendor runs Shopify, which serves its whole catalogue as JSON
at `/products.json`. That makes this far cheaper than the forum scraper: **one
request per vendor** (five of the six catalogues fit in a single page of 250;
KBDfans needs a second), no HTML parsing, and no cover pipeline at all —
Shopify's CDN resizes on demand, so a `_600x` suffix turns an 819 KB product
shot into a 32 KB thumbnail. Nothing is downloaded or thumbnailed locally.

| Vendor | Products | `product_type` quality |
|---|---|---|
| [KBDfans](https://kbdfans.com) | 380 | mixed — layout, switch feel and lifecycle all share the field |
| [Meletrix](https://meletrix.com) | 186 | partial |
| [Omnitype](https://omnitype.com) | 176 | good — Keycaps / Deskpad / Switches |
| [Qwertykeys](https://qwertykeys.com) | 99 | internal codes |
| [Mode Designs](https://modedesigns.com) | 84 | product lines, not categories |
| [Matrix Lab](https://www.matrixlab.store) | 33 | blank |

None of their robots.txt files disallow it, and none declare a crawl-delay.

Categories come from the vendor's own `product_type` where it maps cleanly, and
fall back to the shared classifier otherwise. Three catalogue-specific quirks are
handled in `vendors.py`:

- **A storefront names the board in every one of its spare parts** — "Neo60 Cu
  Weight", "ZOOM65 V3 Add On - External Weight". The forum scraper's size rule
  (which exists so "Shy60" reads as a keyboard) then files the entire parts bin
  under Keyboards, so on a catalogue a part noun outranks it.
- **`configurator` tags are not a noise signal.** Mode tags standalone products
  — Lotus Keycaps, 65% Plate, SixtyFive Weight — as `configurator-component`
  because they double as build options. Filtering on that tag dropped 43 of
  their 85 products. Configurator-only entries are caught by their titles
  instead (`[CFG]`, `Add On`).
- **KBDfans puts three different things in `product_type`** — the layout
  (`60% assembled keyboard`, `80% DIY KIT`, `65% PCB`), the switch feel with no
  noun attached (`Linear`, `Magnetic`), and the lifecycle (`Interest Check`,
  `In Production`). Patterns cover the `%`-parameterised families in three rules
  instead of thirty dict entries. Its tags also only ever accumulate: 37 products
  carry both `Group Buy` and `In Stock`, because a set whose buy closed years ago
  keeps the tag while it sits on the shelf as extras. So where `product_type`
  names a lifecycle it outranks the tags — though `In Stock` there still doesn't
  mean anything is left to buy, so that answer comes from the variants.

Gift cards, deposits, shipping fees and add-on entries are dropped by default;
`--include-all` keeps them. KBDfans marks its add-ons, hidden configurators and
EU-tariff line in `product_type` rather than the title, so those are read there.

### Singakbd

Not supported, and not for a technical reason: **the whole storefront is behind
Shopify's password gate.** `/`, `/collections/all` and `/password` all return
the same 55,864-byte page with `canonical: /password` and zero product links;
`/products.json`, `/collections/all/products.json` and `/cart.js` all 401; there
is no sitemap. No open reseller carries their stock either (checked iLumkb,
Deskhero, KTechs, PantheonKeys, Divinikey). There is no public data to read, so
the only route in would be defeating the shop's password — add it to `VENDORS`
in `vendors.py` if that ever changes.

## Server

```bash
python serve.py --interval 5
python serve.py --interval 5 --vendors            # geekhack + all vendors
python serve.py --vendors omnitype,qwertykeys     # or just some
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

Vendors are polled every cycle regardless, because storefront stock moves
without the forum changing and it is one request each. When the board is
unchanged but vendors are enabled, only the vendors are refetched.

## Android app

A thin client: it renders whatever the server is currently serving, so
improving the feed only means restarting the server, not rebuilding the app.

1. Run `serve.py` and note the address it prints.
2. Install the APK from the [Releases page](../../releases).
3. First launch asks for the server address — `192.168.x.x:8765`.

Menu: **Refresh** reloads the page, **Rescan geekhack now** calls
`/api/refresh` so you do not have to wait out the timer, **Change server**
re-prompts for the address, **Check for updates** asks GitHub. Back navigates
the WebView; thread links open in the real browser.

### Updating itself

The app reads `/releases/latest` from the GitHub API, compares the tag against
its own `versionCode`, and offers to download and install a newer one. It
checks on launch at most once every 6 hours, and the menu item forces a check.
"Skip this one" suppresses a specific tag until a later one appears.

Two things make that work:

- **Version codes come from the tag.** `v1.2.3` becomes `10203` via the same
  formula in `build.py` and `UpdateManager.versionCodeOf`, so the comparison is
  arithmetic rather than string guesswork. `AndroidManifest.xml` deliberately
  declares no `versionCode` — aapt2 only injects the command-line value when
  the manifest is silent, and a manifest-pinned `1` would make every release
  look newer than itself, forever.
- **`UpdateProvider`** hands the downloaded file to the system installer.
  Android refuses a `file://` URI across apps and the usual answer is AndroidX
  `FileProvider`; this is the same idea in the ~50 lines actually needed,
  serving exactly one read-only file, which keeps the app AndroidX-free.

Android also requires the user to allow this app to install packages
(Settings → install unknown apps). The app detects that and offers to open the
right screen.

### Building

CI builds every push and publishes on tags, so you normally do not need to.
Locally:

```powershell
cd android; .\build.ps1
```

That is a wrapper around `build.py`, which is what CI runs too — one
implementation, so a change cannot work locally and break the release job. It
drives `aapt2`, `javac`, `d8`, `zipalign` and `apksigner` directly: no Gradle,
no Android Studio, nothing downloaded. On Windows it finds the SDK and JDK that
ship with Unity; elsewhere it uses `ANDROID_SDK_ROOT` and `JAVA_HOME`. Add
`-Install` to push over adb.

## Releasing

`.github/workflows/release.yml` builds on every push and pull request. Pushing
a `v*` tag also publishes a GitHub Release with the APK attached and notes
generated from the commits since the previous tag.

```bash
git tag v1.0.0 && git push origin v1.0.0
```

### Release signing

**Nothing to configure: the signing key is committed at
`android/public-release.keystore`, and its password is `android`.**

Android refuses to install an APK over one signed with a different key, so the
key a release is signed with has to be *stable* — otherwise no release could
update another and the in-app updater would be useless. Stability is the only
property that matters here, and a key can be stable without being secret, so
this repo publishes one rather than asking anyone to set up secrets. `build.py`
defaults to the same key, which means an APK pushed over adb and an APK
downloaded from Releases can replace each other.

**The trade-off is real: this key is public, so anyone can build an APK that an
install of this app accepts as an update.** There is no store listing and the
updater only reads this repo's Releases, so it takes someone getting you to
sideload their build — but if that matters to you, use a private key instead.

To switch to one, create it and keep it safe (losing it means every install has
to be uninstalled and reinstalled by hand):

```bash
keytool -genkeypair -v -keystore release.jks -alias geekhackfeed \
  -keyalg RSA -keysize 2048 -validity 10000
base64 -w0 release.jks > release.jks.b64
```

On Windows, `keytool` comes with the JDK that Unity ships, and this puts the
base64 straight on the clipboard ready to paste:

```powershell
$jdk = "C:\Program Files\Unity\Hub\Editor\6000.4.7f1\Editor\Data\PlaybackEngines\AndroidPlayer\OpenJDK\bin"
& "$jdk\keytool.exe" -genkeypair -v -keystore release.jks -alias geekhackfeed -keyalg RSA -keysize 2048 -validity 10000
[Convert]::ToBase64String([IO.File]::ReadAllBytes("release.jks")) | Set-Clipboard
```

Then add these under Settings → Secrets and variables → Actions. **Both of the
first two are required to take effect** — a keystore cannot be opened without
its password, so setting only one is ignored (with a warning) rather than
silently producing releases signed with a different key:

| Secret | Value |
|---|---|
| `KEYSTORE_BASE64` | contents of `release.jks.b64` — the base64 *text*, not the file's bytes |
| `KEYSTORE_PASSWORD` | the keystore password |
| `KEY_PASSWORD` | optional; defaults to the keystore password |
| `KEY_ALIAS` | optional; defaults to `geekhackfeed` |

Keep `release.jks` out of the repo — `.gitignore` covers `*.jks` anywhere in
the tree. Switching keys breaks updates for anyone already running a
public-key build, so do it before you hand the app to anyone.

The workflow logs which key it used and prints the certificate's SHA-256 on
every run, so you can confirm at a glance that releases still share one. The
committed key is:

```
SHA256: DB:F7:31:E6:F1:C2:A4:DF:B9:EF:56:44:9F:69:79:12:0F:96:79:3D:BC:D4:1A:E6:49:CD:B5:01:EC:DB:44:36
```

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
