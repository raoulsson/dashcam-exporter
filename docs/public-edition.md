# The public edition — how the configured/unconfigured split works

Someone clones this, points it at their card, and gets rendered trips plus a
browsable page on their own machine. No account, no host, no second repo,
nothing that assumes a particular website exists. Publishing is the optional
half, and this document describes where the seam is.

## Where the seam is

The renderer (`infrastructure/media/renderer.py`) is self-contained — it reads a card and
writes trips into `--out`, and knows nothing about any website. The rest of
this repo knows one interface — an ACT of publishing work, `uploader.Act` — and
one setting that names a file and the two classes in it that implement it: a
`Builder` for item 5 and an `Uploader` for item 8.

So the seam is a **type**, not a set of config keys, and it is worth being
precise about why that matters: config keys describe a destination, and a
destination someone else's tool describes is a destination it has assumptions
about. A type describes a question. Where your videos go, what serves them,
and whether "published" means an object in a bucket or a file on a disk is your
implementation's business and this repo has no opinion about it.

Everything but items 5 and 8 — import, sidecars, preview, exclude, render — is
generic and runs with nothing configured. **With nothing configured, nothing in
this repo talks to a publishing destination.** The renderer does go out for map
material — it fetches OSM tiles for the map burned into each video, and, when
`geocode` is on, asks Nominatim for the place names it writes into
`_meta.json`. Both fail silently, so a render finishes offline with a plain
polyline and no place names.

## What the exporter asks, and what it never asks

| It asks the plugin | It answers for itself |
|---|---|
| Are ALL of this import's trips at the destination? (`is_complete`) | Were these trips rendered on this machine? |
| May this act run, and would it do anything? (`evaluate`) | Is there anything to build a page from? |
| Do it, and what happened? (`execute`) | Is there an import, a sidecar, a GPS track? |
| What does this act do, in one line? (`describe`) | Which trips is the plugin asked about — the exporter's idea of the import, including one that produced no render |
| | Has the operator typed the word? |
| | Which item may follow which — the graph's job |

The right-hand column is the part that never leaves home, and it is not
distrust. An implementation is inside the trust boundary: you chose the class,
the exporter runs it, and it believes what the class says. The exporter simply
does not delegate a question it can already answer. The practical effect is
that an implementation answering yes to everything still cannot talk Clean
Workspace into erasing an import that produced no renders, because that gate
was never the plugin's to answer.

There is one more thing the exporter keeps for itself, and it is what makes an
all-or-nothing answer safe: WHICH trips get named. The list is read off this
import's sidecars, so a trip that was never encoded is in it — the plugin does
not have that trip, answers NO, and the footage that exists nowhere else is not
erased. Asked "do you have everything you were given", a plugin could correctly
say yes about a trip it was never given.

## The shape

**One codebase, not a fork.** A fork means every fix lands twice and drifts.
The public edition is not a different program; it is this one with nothing
supplied. `menu.Strategy` resolves one thing — was an implementation supplied —
and that is the whole branch.

**Disabled, not hidden.** All ten steps are always in the grid at their own
fixed numbers; item 8 is greyed, and `h8` says what it is —

```
    Put what was built online. Not part of this edition.
```

Two reasons, and both are worth more than the tidiness of a shorter menu. The
numbering never shifts between setups, so anything anyone writes about "item 6"
is true on every machine. And the greyed entry is the discovery path: a stranger
can see that publishing exists, in the place they are already looking.

Under the local product item 8 is unreachable for two independent reasons, and
neither is a conditional inside a method: nothing offers it — no item's outbound
set contains 8 — and its own `evaluate` blocks. The strategy is a constructor
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
the outside: the menu looks normal, item 5 writes a local page, item 8 sits
greyed out, and the renders quietly stop reaching the world. The shape check
happens at startup for the same reason — an uploader missing `is_complete()`
would otherwise raise at the moment item 9 asks, which is after the operator
has typed CLEAN.

## The Build Website item

Item 5 builds **what this installation publishes**, and which builder is
installed is the constructor's business, not an `if` in the body.

With nothing configured it writes `dashcam_export_data_site.html` from what the
render already produced, and also GATHERS the render tree into
`final_<day>_<import>` — that is what makes the workspace expendable, and there
is no separate gather item, so it lives here or nowhere.

With a plugin configured, **neither happens**. The page is the local edition's
deliverable and "Nothing leaves this machine" is a sentence about the other
product; and moving the render tree would rename every published trip out from
under whatever index the plugin keeps. The same rule binds the plugin itself:
it reads the workspace and never modifies it, which is the one condition of
trust the interface states and does not police.

The local page invents nothing; every number and every track already exists on
disk as a sidecar next to the mp4, and the poster frame is pulled from the mp4
itself. This pass only arranges them into one page. That is deliberate: it has
to be buildable by someone who has no account, no second repo and no manifest —
it reads the output tree and nothing else.

The page is one self-contained file: every still is embedded, every route is
drawn inline from its GPX as an SVG, and there are no external scripts, fonts
or tiles — it opens from `file://` with no network. Videos are referenced in
place, not copied: a full card is tens of gigabytes, and duplicating it would
cost more disk than the footage is worth. The consequence is that the page is
portable only together with the render tree around it — the
`final_<day>_<import>/` folder that gathers page, videos and sidecars is the
movable unit.

The per-trip `.html` map sidecar is the one artefact that does need the
network: it pulls Leaflet from unpkg and tiles from OSM, so offline it opens as
an empty grey box. That is why the result page draws its routes inline instead
of linking the sidecar as the primary map — the sidecar link stays, for when
the pannable version is wanted.

Deliberately **not** in the local page: invite gates, signed URLs, curation,
access control. Those belong to a published site, and a published site is
whatever your implementation makes it. A local page has no threat model.

## Writing an implementation

The interface states two rules for a destination that has pages and media. Send
the pages first: a trip is publishable as soon as item 2 has described it,
because its route, distance, places and map all come from the sidecars, and
only playback waits on the encode. And treat missing media as an ordinary
outcome rather than a failure — item 5 is reachable before item 6 by design, so
a publish with nothing encoded yet is the normal first move. It completes, and
it says what it did: pages sent, no media to send.

See the README's [Publishing](../README.md#publishing--plugging-in-your-own)
section for the method-by-method table, and
[examples/local_website.py](../examples/local_website.py) for a complete plugin
that stages a site in `/tmp` and sends it, with the transport itself sketched
as pseudo code. The test suite drives the real menu — items 5, 8 and 9,
including the erase gates — through that example, twice, so it cannot rot into
a lie and the already-done path is proven rather than described.
