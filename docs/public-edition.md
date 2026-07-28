# A public edition — design notes

Goal: someone clones this, points it at their card, and gets rendered trips plus
a browsable site on their own machine. No S3, no EC2, no second repo, nothing
that assumes raoulsson.com exists.

## What was actually entangled

This was the diagnosis, and it is now the changelog: every row below is
configurable and unset by default. Kept because it is still the shortest
description of where the private half lives.

Less than it looks. The renderer (`make_dashcam_videos.py`) is already
self-contained — it reads a card and writes trips into `--out`, and knows nothing
about any website. All the coupling is in `pipeline.py`:

| Where | What it reaches for |
|---|---|
| `ctx.site` | the sibling `goodnight-drives` checkout |
| Preview / Render | `build_manifest.py` in that repo, to refresh its index |
| Upload | `deploy/upload-videos-s3.sh`, bucket `your-media-bucket` |
| Deploy | `deploy/deploy-site.sh`, EC2, `SIGNED_VIDEOS=1` |
| Status | `LIVE_TRIPS_URL`, a hardcoded raoulsson.com URL |
| Drop guard | `admin.json` and an S3 listing, to prove a trip was published |

So the private half is four steps and two constants. Everything before them —
import, scan, preview, drop, render — is generic.

## The shape

**One codebase, not a fork.** A fork means every fix lands twice and drifts; and
the public edition is not a different program, it is this one with the private
tail absent. So the private half moved into config.txt, all of it unset by
default:

```
# config.txt — the PUBLISHING section, all optional
#site_repo      = ~/dev/your-site
#s3_bucket      = my-bucket
#s3_region      = eu-central-2
#live_trips_url = https://example.com/trips.json
```

**Disabled, not hidden.** The first draft of this said the private steps would
*appear* only when configured. They do not: every step is always in the menu at
its own fixed number, and the ones whose config is absent are greyed out with
the key that would enable them printed underneath —

```
   7) needs s3_bucket in config.txt
   8) needs site_repo in config.txt
```

Two reasons, and both were worth more than the tidiness of a shorter menu. The
numbering never shifts between setups, so anything anyone writes about "step 5"
is true on every machine. And the greyed line is the discovery path: a stranger
can see that publishing exists and exactly what turns it on, in the place they
are already looking. The mechanism was already there — `NOOP_CHECK` /
`unavailable_steps()`, which greys Import when the sink is already full.

- **nothing set** (the default, and what a clone gets): Import, List, Preview,
  Drop, Render, Site run; Upload and Deploy are greyed. Everything lands under
  `out`, and nothing contacts a network host.
- **`site_repo` set**: Deploy lights up.
- **`site_repo` + `s3_bucket`**: Upload lights up too, and the status screen
  grows the Prepared row; `live_trips_url` adds the Live site row and is the
  only thing that makes the CLI fetch anything at startup.

That also fixes a smaller thing: the CLI used to print "goodnight-drives repo
not found — steps 7-9 will not run" at anyone who did not have it, which is
noise about a repo they have never heard of. It is gone; a disabled step says
for itself why it is disabled.

## The Site step

Builds `<out>/site/` from what the render already produced. Nothing new is
computed; every input exists:

```
<out>/<import>/<day>/trip_*.mp4          the video
                     trip_*.html         Leaflet map (needs the network for tiles)
                     trip_*.gpx          the track
                     trip_*_meta.json    distance, moving time, speeds, stops, bbox
                     trip_*_links.txt    Google/Apple map links
```

Output:

```
<out>/site/index.html      days -> trips, with a still and the numbers
<out>/site/trip-<id>.html  the video, the map, the stats, the places
<out>/site/still/*.jpg     one frame per trip (the poster technique)
```

Relative links throughout, so it works from `file://` and equally from any
static host — which is the "savvy people take it from there" path: copy `site/`
and the mp4s to a web server and it is a website, with no build step.

Deliberately **not** in it: the invite gate, signed URLs, curation, S3. Those
exist because that site is private and served from a bucket. A local site has no
threat model.

## Open questions

1. **The map tiles are the one external dependency.** The sidecar links
   `unpkg.com` for Leaflet and OSM for tiles, so a fully offline site would need
   a static PNG map instead. The renderer already draws one for the video panel
   (`staticmap`), so an offline fallback is available if wanted.
2. **Do the videos get copied into `site/` or referenced in place?** Referencing
   avoids doubling tens of GB; copying makes `site/` a single portable folder.
   Referencing, with a note, seems right.
3. **Naming.** `pipeline.py` is fine but generic; if this is the front door for
   other people it may want to be `dashcam.py` or an entry point in the README.
