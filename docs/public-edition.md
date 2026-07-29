# The public edition — how the configured/unconfigured split works

Someone clones this, points it at their card, and gets rendered trips plus a
browsable page on their own machine. No S3, no EC2, no second repo, nothing
that assumes a particular website exists. Publishing is the optional half, and
this document describes where the seam is.

## Where the private half lives

The renderer (`make_dashcam_videos.py`) is self-contained — it reads a card and
writes trips into `--out`, and knows nothing about any website. All the
coupling to a publishing setup is in `pipeline.py`, and every piece of it is a
config value that is unset by default:

| Where | What it reaches for | Gated by |
|---|---|---|
| `ctx.site` | a sibling site checkout | `site_repo` |
| Preview / Render | `build_manifest.py` in that repo, to refresh its index | `site_repo` |
| Upload (7) | `deploy/upload-videos-s3.sh` | `s3_bucket` + `site_repo` |
| Update site (8) | `deploy/deploy-site.sh` | `site_repo` |
| Status | the live `trips.json` | `live_trips_url` |
| Exclude-trip guard | `admin.json` and an S3 listing, to prove a trip was published | `site_repo` / `s3_bucket` |

So the private half is a handful of steps keyed off a few settings. Everything
before them — import, scan, preview, exclude, render, local website — is
generic and runs with nothing configured. `live_trips_url` is the only thing
that makes the CLI touch the network at startup; unset, nothing is fetched.

## The shape

**One codebase, not a fork.** A fork means every fix lands twice and drifts.
The public edition is not a different program; it is this one with the private
tail absent. The private half lives in config (`site_repo`, `s3_bucket`,
`s3_region`, `live_trips_url`, all optional — real values in the gitignored
`.env`), and absence of the config is what makes an install "public".

**Disabled, not hidden.** Every step is always in the menu at its own fixed
number; the ones whose config is absent are greyed out with the key that would
enable them printed underneath —

```
   7) needs s3_bucket in config.txt
   8) needs site_repo in config.txt
```

Two reasons, and both are worth more than the tidiness of a shorter menu. The
numbering never shifts between setups, so anything anyone writes about "step 5"
is true on every machine. And the greyed line is the discovery path: a stranger
can see that publishing exists and exactly what turns it on, in the place they
are already looking. The same mechanism (`NOOP_CHECK` / `unavailable_steps()`)
greys Import when the sink is already full, and it is recomputed on every menu
draw, so a step comes back the moment the world changes.

An unconfigured install never sees a warning about a repo it has never heard
of: a disabled step says for itself why it is disabled, and that is the only
place the missing config is mentioned.

- **nothing set** (the default, and what a clone gets): Import, List, Preview,
  Exclude, Render and Create website run; Upload and Update site are greyed.
  Everything lands under `out`, and nothing contacts a network host.
- **`site_repo` set**: Update site lights up.
- **`site_repo` + `s3_bucket`**: Upload lights up too, and the status screen
  grows the Prepared row; `live_trips_url` adds the Live site row.

## The Create website step

Builds `dashcam_import_data_site.html` from what the render already produced.
Nothing new is computed; every number, still and track already exists on disk
as a sidecar next to the mp4, and this pass only arranges them into one page.
That is deliberate: the page has to be buildable by someone who has no S3
account, no second repo and no manifest — it reads the output tree and nothing
else, and never calls `build_manifest`, lists a bucket, or opens `admin.json`.

The page is one self-contained file: every still is embedded, every route is
drawn inline from its GPX as an SVG, and there are no external scripts, fonts
or tiles — it opens from `file://` with no network. Videos are referenced in
place, not copied: a full card is tens of gigabytes, and duplicating it would
cost more disk than the footage is worth. The consequence is that the page is
portable only together with the render tree around it — the `final_<date>/`
folder that gathers page, videos and sidecars is the movable unit.

The per-trip `.html` map sidecar is the one artefact that does need the
network: it pulls Leaflet from unpkg and tiles from OSM, so offline it opens as
an empty grey box. That is why the result page draws its routes inline instead
of linking the sidecar as the primary map — the sidecar link stays, for when
the pannable version is wanted.

Deliberately **not** in the local page: the invite gate, signed URLs, curation,
S3. Those exist because the published site is private and served from a bucket.
A local page has no threat model.
