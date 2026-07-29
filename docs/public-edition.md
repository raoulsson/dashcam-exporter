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
| Render / Exclude Trip | `build_manifest.py` in that repo, to refresh its index | `site_repo` |
| Upload Website (7) | `deploy/upload-videos-s3.sh`, then `deploy/deploy-site.sh` | `s3_bucket` + `site_repo` |
| Clean Workspace (8) | `deploy/is-complete.py`, to prove the footage may go | `site_repo` |
| Status | the live `trips.json` | `live_trips_url` |
| Exclude Trip's last-copy warning | `admin.json` and an S3 listing, to prove a trip was published | `site_repo` / `s3_bucket` |

So the private half is ONE menu item keyed off two settings. Item 7 drives both
transports because they are one job — assets to the bucket, pages to the server
— and splitting them was how a deploy could go out over videos that had not
landed. Everything before it — import, sidecars, preview, exclude, render,
local website — is generic and runs with nothing configured. `live_trips_url` is the only thing
that makes the CLI touch the network at startup; unset, nothing is fetched.

## The shape

**One codebase, not a fork.** A fork means every fix lands twice and drifts.
The public edition is not a different program; it is this one with the private
tail absent. The private half lives in config (`site_repo`, `s3_bucket`,
`s3_region`, `live_trips_url`, all optional — real values in the gitignored
`.env`), and absence of the config is what makes an install "public".

**Disabled, not hidden.** All ten items are always in the menu at their own
fixed numbers; the ones whose config is absent are greyed out with the key that
would enable them printed underneath —

```
   7) needs s3_bucket in config.txt
```

Two reasons, and both are worth more than the tidiness of a shorter menu. The
numbering never shifts between setups, so anything anyone writes about "item 5"
is true on every machine. And the greyed line is the discovery path: a stranger
can see that publishing exists and exactly what turns it on, in the place they
are already looking.

Under the local product item 7 is unreachable for two independent reasons, and
neither is a conditional inside a method: nothing offers it — no item's outbound
set contains 7 — and its own `evaluate` blocks. The strategy is a constructor
argument, resolved once when the menu is built, so which edges an item reports
and which collaborator it runs are settled there rather than re-tested on every
draw.

The same mechanism greys any item whose evidence is missing — Import when there
is no source and nothing imported, Delete SIM Data when there is no card — and
it is recomputed from a freshly captured world on every menu draw, so an item
comes back the moment the world changes.

An unconfigured install never sees a warning about a repo it has never heard
of: a disabled item says for itself why it is disabled, and that is the only
place the missing config is mentioned.

- **nothing set** (the default, and what a clone gets): Progress, Import SIM,
  Generate Meta, Build Preview, Exclude Trip, Render Videos, Build Website,
  Clean Workspace and Delete SIM Data run; Upload Website is greyed.
  Everything lands under `out`, and nothing contacts a network host.
- **`site_repo` + `s3_bucket`**: Upload Website lights up — both settings, because
  it is one job with two transports and half of it publishes nothing usable. The
  status screen grows the Prepared row; `live_trips_url` adds the Live site row.

## The Build Website item

Builds `dashcam_import_data_site.html` from what the render already produced.
Under the local product it also GATHERS the render tree into `final_<day>_<import>`,
which is what makes the workspace expendable — there is no separate gather item,
so it lives here or nowhere. Which gatherer is installed is the constructor's
business: under the publishing product it moves nothing, because `trips.json`
records each trip by a uid containing its import folder name and moving the tree
would orphan every published trip.
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
