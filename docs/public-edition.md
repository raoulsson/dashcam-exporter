# A public edition — design notes

Goal: someone clones this, points it at their card, and gets rendered trips plus
a browsable site on their own machine. No S3, no EC2, no second repo, nothing
that assumes raoulsson.com exists.

## What is actually entangled

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
tail absent. So: the private steps appear only when a site repo is configured.

```
# config.txt
#site_repo = ~/dev/your-site     # unset in the public edition
```

- **unset** (the default, and what a clone gets): Import, List, Preview, Drop,
  Render, Site. Everything lands under `out`.
- **set and present**: the four private steps appear as they do today.

That also fixes a smaller thing: today the CLI prints "goodnight-drives repo not
found — steps 6-8 will not run" at anyone who does not have it, which is noise
about a repo they have never heard of.

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
