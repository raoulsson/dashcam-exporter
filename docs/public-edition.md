# The public edition — how the configured/unconfigured split works

Someone clones this, points it at their card, and gets rendered trips plus a
browsable page on their own machine. No account, no host, no second repo,
nothing that assumes a particular website exists. Publishing is the optional
half, and this document describes where the seam is.

## Where the seam is

The renderer (`make_dashcam_videos.py`) is self-contained — it reads a card and
writes trips into `--out`, and knows nothing about any website. The rest of
this repo used to reach for one operator's arrangement in nine places: a
sibling checkout, a bucket, two deploy scripts, a manifest builder and a live
URL. All of it is gone. What replaced it is one interface,
`uploader.WebsiteUploaderInterface`, and one setting that names a class
implementing it.

So the seam is a **type**, not a set of config keys, and it is worth being
precise about why that matters: config keys describe a destination, and a
destination someone else's tool describes is a destination it has assumptions
about. A type describes a question. Where your videos go, what serves them,
and whether "published" means an object in a bucket or a file on a disk is your
implementation's business and this repo has no opinion about it.

Everything before item 6 — import, sidecars, preview, exclude, render — is
generic and runs with nothing configured. **With nothing configured, no code in
this repo contacts a network host at any point.** That used to be a property
maintained by one setting being unset; it is now a property of there being no
networked code left here to run.

## What the exporter asks, and what it never asks

| It asks the target | It answers for itself |
|---|---|
| Is this render at the destination, at this size? (`holds`) | Were these trips rendered on this machine? |
| Is it actually being served? (`published`) | Is there anything to build a page from? |
| Would an upload still do anything? (`owes`) | Is there an import, a sidecar, a GPS track? |
| Is anything there for this trip id? (`carries`) | Has the operator typed the word? |
| May build/upload run, and why not? (`why_not_*`) | Which item may follow which — the graph's job |

The right-hand column is the part that never leaves home, and it is not
distrust. An implementation is inside the trust boundary: you chose the class,
the exporter runs it, and it believes what the class says. The exporter simply
does not delegate a question it can already answer. The practical effect is
that an implementation answering yes to everything still cannot talk Clean
Workspace into erasing an import that produced no renders, because that gate
was never the target's to answer.

## The shape

**One codebase, not a fork.** A fork means every fix lands twice and drifts.
The public edition is not a different program; it is this one with nothing
supplied. `menu.Strategy` used to resolve "site repo and bucket are both set",
which is one operator's config keys standing in for a question. It now resolves
one thing — was an implementation supplied — and that is the whole branch.

**Disabled, not hidden.** All ten items are always in the menu at their own
fixed numbers; item 7 is greyed out with the reason printed underneath —

```
   7) not part of this edition
```

Two reasons, and both are worth more than the tidiness of a shorter menu. The
numbering never shifts between setups, so anything anyone writes about "item 5"
is true on every machine. And the greyed line is the discovery path: a stranger
can see that publishing exists, in the place they are already looking.

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

## A configured implementation that will not load stops the tool

Loudly, before the menu is drawn, with the reason and no fallback. Never a
quiet degradation to the local edition, because that failure is invisible from
the outside: the menu looks normal, item 6 writes a local page, item 7 sits
greyed out, and the renders quietly stop reaching the world. The shape check
happens at startup for the same reason — an implementation missing
`published()` would otherwise raise at the moment item 8 asks, which is after
the operator has typed CLEAN.

## The Build Website item

Item 6 builds **what this installation publishes**, and which builder is
installed is the constructor's business, not an `if` in the body.

With nothing configured it writes `dashcam_export_data_site.html` from what the
render already produced, and also GATHERS the render tree into
`final_<day>_<import>` — that is what makes the workspace expendable, and there
is no separate gather item, so it lives here or nowhere.

With an uploader configured, **neither happens**. The page is the local
edition's deliverable and "Nothing leaves this machine" is a sentence about the
other product; and moving the render tree would rename every published trip out
from under whatever index the target keeps. This used to be a bug: only the
*mover* was the strategy branch, so the page writer ran either way and a
publishing install got a local page it never asked for, announcing that nothing
had left the machine while item 7 was about to send it all.

The local page computes nothing new; every number, still and track already
exists on disk as a sidecar next to the mp4, and this pass only arranges them
into one page. That is deliberate: it has to be buildable by someone who has no
account, no second repo and no manifest — it reads the output tree and nothing
else.

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

Deliberately **not** in the local page: invite gates, signed URLs, curation,
access control. Those belong to a published site, and a published site is
whatever your implementation makes it. A local page has no threat model.

## Writing an implementation

See the README's [Publishing](../README.md#publishing--plugging-in-your-own)
section for the method-by-method table, and
[examples/uploader_folder.py](../examples/uploader_folder.py) for a complete
one that publishes to a folder. The test suite drives the real menu — items 6,
7 and 8, including the erase gates — through that example, so it cannot rot
into a lie.
