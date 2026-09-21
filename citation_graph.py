"""
Sample 05 - Efficient citation graph builder using batch expansion.

Same goal as 04_build_citation_graph.py (BFS outward from a seed paper), but
uses /paper/batch to expand an entire graph level in ONE request instead of
one references/citations call per node. Call count is O(depth), not O(nodes).

How the single-request-per-level trick works
----------------------------------------------
/paper/{id} and /paper/batch both accept nested fields like
`references.title`, `citations.authors`, etc. Requesting those fields returns
up to 1,000 references AND up to 1,000 citations (each capped separately) for
every paper in the same response that fetches the paper's own metadata. So:

  1 call to /paper/{seed}   with references.* + citations.*
      -> seed metadata + ALL of level 1, already hydrated.

  1 batch call for up to 500 level-1 paper IDs, requesting references.*
  (and citations.* for the citation-track ids, see below)
      -> level-1 papers' own metadata (already have it) + ALL of level 2.

No per-node /citations or /references calls are made at all.

Asymmetric expansion rule (as requested)
----------------------------------------------
Level 1 splits into two tracks depending on which edge reached the seed:
  * reference-track  (seed -> X, i.e. X is something the seed cites)
        Only follow X's own REFERENCES further out (pure ancestry chain).
  * citation-track    (X -> seed, i.e. X cites the seed)
        Follow X's CITATIONS *and* REFERENCES (grows both ways from there).
This rule is applied at every subsequent level too: a node's track never
changes once assigned, so "reference-track" nodes only ever expand further
via references, while "citation-track" nodes always expand via both.

Fan-out control + author-overlap ranking
----------------------------------------------
The API doesn't support `limit` on nested reference/citation lists (you get
up to 1,000 back), so each node's neighbor list is truncated client-side to
`per_node` before it's added to the next frontier. Truncation order:
  1. shares an author with the seed paper (uses `authors` already present
     in the response -- no extra API calls needed for this)
  2. citationCount, descending
(`isInfluential` is only exposed by the dedicated /citations and
/references endpoints, not as a nested/batch paper field, so it isn't
available for ranking here -- see the note above REF_FIELDS/CIT_FIELDS.)
This keeps the crawl fast (author overlap is free -- it's just re-sorting
data already fetched) while surfacing the most relevant neighbors.

Directed-edge dedup
----------------------------------------------
Edges are (source, target) pairs meaning "source cites target". The same
edge can be rediscovered from two different expansions (e.g. a level-2 node
that is also a level-1 node); it is stored once in a set before being
written out.

Concurrency (two independent layers)
----------------------------------------------
1. Within build(): at each level, the reference-track batch call(s) and the
   citation-track batch call(s) don't depend on each other (and neither do
   further /paper/batch chunks within a track, if a track's frontier exceeds
   BATCH_CHUNK). These are all network-bound `requests` calls, so a small
   `concurrent.futures.ThreadPoolExecutor` runs them in parallel instead of
   sequentially, cutting the wall-clock time per level. build() also accepts
   an optional `on_level_complete(level, nodes_so_far, edges_so_far)`
   callback invoked after each level finishes, so a caller can react to
   partial progress instead of waiting for the whole crawl to return.

2. Around build(): calling build() directly still blocks whatever thread
   calls it for the whole crawl. GraphBuildWorker (a QObject meant to be
   moved to a QThread) wraps build() so a PyQt6 GUI can run it off the main
   /event-loop thread and receive progress/results via Qt signals instead of
   blocking the UI. See GraphBuildWorker below.

Run:
  python3 citation_graph.py "graph neural networks"
  python3 citation_graph.py ARXIV:1706.03762 --depth 3 --per-node 8
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import s2client as s2

# Fields requested for a paper's own metadata (used for seed + every node).
# authors + referenceCount are included so the Viewer UI can label nodes
# with an author and show reference/citation counts in the detail panel.
# url/openAccessPdf/externalIds are included so the detail panel can link
# out to the paper (Semantic Scholar page, an open-access PDF if one
# exists, and/or its DOI) -- see _record_node and _paper_link below.
META_FIELDS = "title,year,citationCount,referenceCount,authors," \
              "url,openAccessPdf,externalIds"

# Nested fields for one level of expansion. `authors` on each neighbor is
# needed (for free, same response) to rank by shared authorship with seed;
# referenceCount/url/openAccessPdf/externalIds are needed for the same UI
# reasons as META_FIELDS above.
# NOTE: `isInfluential` is NOT a valid sub-field here -- it only exists on
# the dedicated /paper/{id}/references and /paper/{id}/citations endpoints
# (as a property of the citation edge, not the paper). The batch/nested
# fields only expose paper-level attributes, so ranking here uses
# shared-author-with-seed + citationCount (see _rank_key).
REF_FIELDS = "references.title,references.year,references.citationCount," \
             "references.referenceCount,references.authors," \
             "references.url,references.openAccessPdf,references.externalIds"
CIT_FIELDS = "citations.title,citations.year,citations.citationCount," \
             "citations.referenceCount,citations.authors," \
             "citations.url,citations.openAccessPdf,citations.externalIds"

# Edge-level fields, only available on the dedicated /paper/{id}/references
# and /paper/{id}/citations endpoints (NOT as nested/batch paper fields --
# confirmed against the live API: isInfluential/contexts/intents are
# properties of the citation edge, not the paper, so /paper/batch and the
# nested references.*/citations.* fields above never expose them). Used
# ONLY for the seed paper (see _fetch_seed_influence) -- fetching this for
# every node at every level would reintroduce one-call-per-node cost and
# defeat the whole point of this module. This keeps the crawl at O(depth)
# calls with a fixed +2 one-time cost for seed-adjacent influence data.
#
# NOTE: `paperId` MUST be included here. Confirmed against the live API --
# requesting only edge-level fields (contexts/intents/isInfluential) with no
# paper-level field at all causes the response to omit the nested
# citedPaper/citingPaper object entirely; at least one paper-level field
# (paperId is the cheapest) has to be requested for that object to appear.
EDGE_FIELDS = "paperId,contexts,intents,isInfluential"

BATCH_CHUNK = 500  # /paper/batch hard limit on ids per call
LEVEL_WORKERS = 4  # max concurrent /paper/batch calls per level (network-bound)
EDGE_LIMIT = 1000  # /references and /citations page cap (matches nested-field cap)
INFLUENCE_POOL_MULTIPLIER = 4  # top-k pool size = per_node * this, before ranking

REFERENCE_TRACK = "reference"  # only ever expands via its own references
CITATION_TRACK = "citation"    # expands via both references and citations

# Called after each level completes: (level, nodes_so_far, edges_so_far) -> None
ProgressCallback = Callable[[int, int, int], None]

# Called after each level completes with the ACTUAL data added that level:
# (level, new_node_dicts, new_edge_dicts) -> None. Unlike ProgressCallback
# above (which only reports running totals), this carries the specific
# papers/edges discovered at this level, tagged with the track each new node
# belongs to -- everything a caller needs to draw the new pieces of the
# graph without re-deriving anything. new_edge_dicts items are
# {"source": ..., "target": ...} in the same shape build()'s return value
# uses. new_node_dicts items are the node dict (see _record_node) plus a
# "_track" key ("reference" or "citation") for layout/expansion purposes.
LevelDataCallback = Callable[[int, list, list], None]


def resolve_seed(term: str) -> str | None:
    """If `term` looks like an ID, use it as-is. Otherwise relevance-search it."""
    id_prefixes = ("ARXIV:", "DOI:", "CorpusId:", "MAG:", "ACL:",
                   "PMID:", "PMCID:", "URL:")
    if term.startswith(id_prefixes) or (len(term) == 40 and " " not in term):
        return term
    res = s2.get("/paper/search", {"query": term, "fields": "title", "limit": 1})
    data = res.get("data") or []
    if not data:
        return None
    print(f"Resolved '{term}' -> {data[0]['paperId']}  ({data[0].get('title')})")
    return data[0]["paperId"]


def _author_ids(paper: dict) -> set[str]:
    return {a["authorId"] for a in (paper.get("authors") or []) if a.get("authorId")}


def _rank_key(nb: dict, seed_authors: set[str],
              influential_ids: Optional[set[str]] = None):
    """Sort key: seed-adjacent influence first (if available), then
    shared-author-with-seed, then citations, descending. Tuple sorted
    descending (via reverse=True in sorted()).

    `influential_ids`, when given, is a bounded top-k set of paperIds
    confirmed isInfluential=True via the dedicated edge endpoint for the
    SEED only (see _fetch_seed_influence/_top_k_influential below) --
    callers ranking level-2+ neighbors don't have this data (getting it
    there would cost one call per node) and simply omit the argument, which
    degrades to the original shared-author + citationCount behavior
    exactly."""
    is_influential = bool(influential_ids and nb.get("paperId") in influential_ids)
    shares_author = bool(_author_ids(nb) & seed_authors)
    return (
        is_influential,
        shares_author,
        nb.get("citationCount") or 0,
    )


def _extract_neighbors(paper: dict, edge_field: str) -> list[dict]:
    """Pull a paper's nested `references` or `citations` list into flat dicts
    carrying paperId/title/year/citationCount/authors."""
    return [row for row in (paper.get(edge_field) or [])
            if row and row.get("paperId")]


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _record_node(nodes: dict, paper: dict) -> None:
    pid = paper.get("paperId")
    if not pid:
        return
    nodes.setdefault(pid, {
        "paperId": pid,
        "title": paper.get("title"),
        "year": paper.get("year"),
        "citationCount": paper.get("citationCount"),
        "referenceCount": paper.get("referenceCount"),
        "authors": paper.get("authors") or [],  # [{authorId, name}, ...]
        "url": paper.get("url"),  # Semantic Scholar page for the paper
        "openAccessPdf": paper.get("openAccessPdf"),  # {url, status, license} or None
        "externalIds": paper.get("externalIds") or {},  # {"DOI": ..., "ArXiv": ..., ...}
    })


def _record_edges(edges: set[tuple[str, str]],
                   citing_id: str, cited_neighbors: list[dict],
                   citing_neighbors: list[dict]) -> None:
    """citing->cited for references; citer->this for citations."""
    for ref in cited_neighbors:
        edges.add((citing_id, ref["paperId"]))
    for cite in citing_neighbors:
        edges.add((cite["paperId"], citing_id))


def _fetch_seed_influence(seed_id: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """One call each to the dedicated /references and /citations endpoints
    for the SEED ONLY, returning `{paperId: {isInfluential, contexts,
    intents}}` maps -- one for the seed's reference side, one for its
    citation side. These are the only two calls in build() that hit the
    per-edge endpoints instead of the nested/batch paper fields; they add a
    fixed +2 calls total, independent of depth/per_node/graph size, so
    build() stays O(depth) overall.

    `limit=EDGE_LIMIT` (1000) matches the cap already assumed for the
    nested references.*/citations.* fields on /paper/{id} -- confirmed
    against the live API to return everything in one page (no `next`
    cursor) for papers within that range. Papers with more than 1000
    references or citations simply aren't covered by the influence boost
    beyond that cap; ranking falls back to citationCount/shared-authorship
    for anything not present in the returned map.
    """
    ref_resp = s2.get(f"/paper/{seed_id}/references",
                       {"fields": EDGE_FIELDS, "limit": EDGE_LIMIT})
    cit_resp = s2.get(f"/paper/{seed_id}/citations",
                       {"fields": EDGE_FIELDS, "limit": EDGE_LIMIT})
    ref_map = {row["citedPaper"]["paperId"]: row
               for row in (ref_resp.get("data") or [])
               if row.get("citedPaper", {}).get("paperId")}
    cit_map = {row["citingPaper"]["paperId"]: row
               for row in (cit_resp.get("data") or [])
               if row.get("citingPaper", {}).get("paperId")}
    return ref_map, cit_map


def _top_k_influential(edge_map: dict[str, dict], k: int) -> set[str]:
    """Reduce a (possibly ~1000-entry) edge map down to a bounded top-k set
    of paperIds, for use as `_rank_key`'s `influential_ids` argument.

    isInfluential is a boolean flag, not a continuous score, so there is no
    finer-grained "most influential" ordering to sort by (confirmed: the
    dedicated endpoints have no `sort` param and return edges in arbitrary
    order). "Top-k by influence" therefore means: every isInfluential=True
    edge, up to k of them (arbitrary order among ties -- the API gives no
    way to break ties more precisely), and if fewer than k are influential,
    backfill the remainder with non-influential ones so the returned set
    still has up to k members for _rank_key to test membership against.

    This bounds the ranking pool to a small set instead of testing
    membership against the full ~1000-row map for every candidate.
    """
    if k <= 0 or not edge_map:
        return set()
    influential = [pid for pid, info in edge_map.items() if info.get("isInfluential")]
    if len(influential) >= k:
        return set(influential[:k])
    non_influential = [pid for pid in edge_map if pid not in influential]
    return set(influential + non_influential[:k - len(influential)])


def _fetch_reference_track_chunk(chunk: list[str]) -> list[dict]:
    """One /paper/batch call for a chunk of reference-track ids (references only)."""
    return s2.post("/paper/batch", chunk, {"fields": f"{META_FIELDS},{REF_FIELDS}"})


def _fetch_citation_track_chunk(chunk: list[str]) -> list[dict]:
    """One /paper/batch call for a chunk of citation-track ids (both directions)."""
    return s2.post("/paper/batch", chunk,
                    {"fields": f"{META_FIELDS},{REF_FIELDS},{CIT_FIELDS}"})


def _record_new(nodes: dict, edges: set[tuple[str, str]], nb: dict,
                 level_new_pids: Optional[list[str]],
                 level_new_edges: Optional[list[tuple[str, str]]]) -> None:
    """_record_node/_record_edges, but also appends to the level's "new this
    level" trackers -- only for items that weren't already present."""
    pid = nb.get("paperId")
    if pid and level_new_pids is not None and pid not in nodes:
        level_new_pids.append(pid)
    _record_node(nodes, nb)


def _record_new_edge(edges: set[tuple[str, str]], edge: tuple[str, str],
                      level_new_edges: Optional[list[tuple[str, str]]]) -> None:
    if level_new_edges is not None and edge not in edges:
        level_new_edges.append(edge)
    edges.add(edge)


def _process_reference_track_paper(paper: dict, seed_authors: set[str], per_node: int,
                                    nodes: dict, edges: set[tuple[str, str]],
                                    next_frontier: list[tuple[str, str]],
                                    level_new_pids: Optional[list[str]] = None,
                                    level_new_edges: Optional[list[tuple[str, str]]] = None
                                    ) -> None:
    if not paper or not paper.get("paperId"):
        return
    nbs = _extract_neighbors(paper, "references")
    nbs.sort(key=lambda nb: _rank_key(nb, seed_authors), reverse=True)
    nbs = nbs[:per_node]
    for nb in nbs:
        _record_new(nodes, edges, nb, level_new_pids, level_new_edges)
        _record_new_edge(edges, (paper["paperId"], nb["paperId"]), level_new_edges)
    next_frontier += [(nb["paperId"], REFERENCE_TRACK) for nb in nbs]


def _process_citation_track_paper(paper: dict, seed_authors: set[str], per_node: int,
                                   nodes: dict, edges: set[tuple[str, str]],
                                   next_frontier: list[tuple[str, str]],
                                   level_new_pids: Optional[list[str]] = None,
                                   level_new_edges: Optional[list[tuple[str, str]]] = None
                                   ) -> None:
    if not paper or not paper.get("paperId"):
        return
    ref_nbs = _extract_neighbors(paper, "references")
    cit_nbs = _extract_neighbors(paper, "citations")
    ref_nbs.sort(key=lambda nb: _rank_key(nb, seed_authors), reverse=True)
    cit_nbs.sort(key=lambda nb: _rank_key(nb, seed_authors), reverse=True)
    ref_nbs = ref_nbs[:per_node]
    cit_nbs = cit_nbs[:per_node]
    for nb in ref_nbs:
        _record_new(nodes, edges, nb, level_new_pids, level_new_edges)
        _record_new_edge(edges, (paper["paperId"], nb["paperId"]), level_new_edges)
    for nb in cit_nbs:
        _record_new(nodes, edges, nb, level_new_pids, level_new_edges)
        _record_new_edge(edges, (nb["paperId"], paper["paperId"]), level_new_edges)
    next_frontier += [(nb["paperId"], REFERENCE_TRACK) for nb in ref_nbs]
    next_frontier += [(nb["paperId"], CITATION_TRACK) for nb in cit_nbs]


def build(seed_id: str, depth: int, per_node: int,
          on_level_complete: Optional[ProgressCallback] = None,
          on_level_data: Optional[LevelDataCallback] = None,
          use_seed_influence: bool = True) -> dict:
    """
    Crawl outward from `seed_id` up to `depth` levels, keeping at most
    `per_node` neighbors per direction per paper (see module docstring for
    the ranking and track rules).

    If `on_level_complete` is given, it's called as
    `on_level_complete(level, len(nodes), len(edges))` after level 1 and
    after every subsequent level -- useful for a caller (e.g. a Qt worker)
    that wants to report progress instead of blocking until the full crawl
    at `depth` returns.

    If `on_level_data` is given, it's called as
    `on_level_data(level, new_nodes, new_edges)` with the actual node/edge
    dicts added during that level (see LevelDataCallback), so a caller that
    wants to draw the graph incrementally doesn't have to diff the running
    totals itself.

    If `use_seed_influence` is True (default), two extra calls are made
    up front to the dedicated /paper/{seed}/references and
    /paper/{seed}/citations endpoints (see _fetch_seed_influence), and
    level-1 neighbors are ranked with real isInfluential data ahead of
    shared-authorship/citationCount. This only affects the SEED's direct
    neighbors -- levels 2+ always rank by shared-authorship/citationCount
    only, since fetching influence data there would cost one call per node
    and break the O(depth) call-cost guarantee this module is built around.
    Set to False to skip the extra 2 calls and fall back to the original
    ranking exactly (e.g. for a caller that wants the cheapest possible
    single-level-1 crawl).
    """
    nodes: dict[str, dict] = {}
    edges: set[tuple[str, str]] = set()

    # --- Level 0 -> Level 1: single call, nested references + citations ---
    seed = s2.get(f"/paper/{seed_id}",
                  {"fields": f"{META_FIELDS},{REF_FIELDS},{CIT_FIELDS}"})
    _record_node(nodes, seed)
    seed_authors = _author_ids(seed)

    if on_level_data:
        seed_node = dict(nodes[seed["paperId"]])
        seed_node["_track"] = "seed"
        on_level_data(0, [seed_node], [])

    ref_neighbors = _extract_neighbors(seed, "references")
    cit_neighbors = _extract_neighbors(seed, "citations")

    # +2 one-time calls: real isInfluential/contexts/intents for the seed's
    # own references/citations, reduced to a bounded top-k pool so ranking
    # doesn't test membership against the full ~1000-row response. See
    # _fetch_seed_influence/_top_k_influential docstrings for details.
    ref_influence: dict[str, dict] = {}
    cit_influence: dict[str, dict] = {}
    ref_top_k: set[str] = set()
    cit_top_k: set[str] = set()
    if use_seed_influence:
        ref_influence, cit_influence = _fetch_seed_influence(seed["paperId"])
        pool_size = per_node * INFLUENCE_POOL_MULTIPLIER
        ref_top_k = _top_k_influential(ref_influence, pool_size)
        cit_top_k = _top_k_influential(cit_influence, pool_size)

    ref_neighbors.sort(key=lambda nb: _rank_key(nb, seed_authors, ref_top_k), reverse=True)
    cit_neighbors.sort(key=lambda nb: _rank_key(nb, seed_authors, cit_top_k), reverse=True)
    ref_neighbors = ref_neighbors[:per_node]
    cit_neighbors = cit_neighbors[:per_node]

    for nb in ref_neighbors + cit_neighbors:
        _record_node(nodes, nb)
    _record_edges(edges, seed["paperId"], ref_neighbors, cit_neighbors)

    # frontier: (paperId, track) for level 1
    frontier = [(nb["paperId"], REFERENCE_TRACK) for nb in ref_neighbors] + \
               [(nb["paperId"], CITATION_TRACK) for nb in cit_neighbors]
    frontier = list(dict.fromkeys(frontier))  # dedupe, preserve order

    print(f"Level 1: {len(ref_neighbors)} reference(s), {len(cit_neighbors)} "
          f"citation(s) -- {len(frontier)} unique frontier node(s)")
    if on_level_complete:
        on_level_complete(1, len(nodes), len(edges))
    if on_level_data:
        level1_nodes = [dict(nodes[pid], _track=track) for pid, track in frontier]
        level1_edges = []
        for nb in ref_neighbors:
            e = {"source": seed["paperId"], "target": nb["paperId"]}
            info = ref_influence.get(nb["paperId"])
            if info:
                e["isInfluential"] = info.get("isInfluential")
                e["intents"] = info.get("intents") or []
            level1_edges.append(e)
        for nb in cit_neighbors:
            e = {"source": nb["paperId"], "target": seed["paperId"]}
            info = cit_influence.get(nb["paperId"])
            if info:
                e["isInfluential"] = info.get("isInfluential")
                e["intents"] = info.get("intents") or []
            level1_edges.append(e)
        on_level_data(1, level1_nodes, level1_edges)

    # --- Levels 2..depth: batch calls per level, dispatched concurrently ---
    # Within a level, every chunk (either track, and every 500-id split of a
    # track) is an independent /paper/batch call -- a network-bound request
    # that doesn't depend on any other chunk's result. A small thread pool
    # runs them in parallel instead of one-by-one, since the bottleneck is
    # waiting on the API, not CPU work.
    with ThreadPoolExecutor(max_workers=LEVEL_WORKERS) as pool:
        for level in range(2, depth + 1):
            if not frontier:
                break

            ref_track_ids = [pid for pid, track in frontier if track == REFERENCE_TRACK]
            cit_track_ids = [pid for pid, track in frontier if track == CITATION_TRACK]
            next_frontier: list[tuple[str, str]] = []
            # Track which paperIds/edges are newly discovered THIS level, so
            # on_level_data can report exactly what changed (a node/edge may
            # already exist in `nodes`/`edges` from an earlier level -- those
            # are not re-reported here).
            level_new_pids: list[str] = []
            level_new_edges: list[tuple[str, str]] = []

            # Submit every chunk from both tracks up front; they're independent.
            ref_futures = [pool.submit(_fetch_reference_track_chunk, chunk)
                           for chunk in _chunks(ref_track_ids, BATCH_CHUNK)]
            cit_futures = [pool.submit(_fetch_citation_track_chunk, chunk)
                           for chunk in _chunks(cit_track_ids, BATCH_CHUNK)]

            for future in ref_futures:
                for paper in future.result():
                    _process_reference_track_paper(
                        paper, seed_authors, per_node, nodes, edges,
                        next_frontier, level_new_pids, level_new_edges)
            for future in cit_futures:
                for paper in future.result():
                    _process_citation_track_paper(
                        paper, seed_authors, per_node, nodes, edges,
                        next_frontier, level_new_pids, level_new_edges)

            frontier = list(dict.fromkeys(next_frontier))
            print(f"Level {level}: {len(frontier)} unique frontier node(s), "
                  f"{len(nodes)} total node(s) so far")
            if on_level_complete:
                on_level_complete(level, len(nodes), len(edges))
            if on_level_data:
                track_by_pid = dict(frontier)
                new_nodes = [dict(nodes[pid], _track=track_by_pid.get(pid, "reference"))
                             for pid in dict.fromkeys(level_new_pids)]
                new_edges = [{"source": s, "target": t} for s, t in level_new_edges]
                on_level_data(level, new_nodes, new_edges)

    edge_list = [{"source": s, "target": t} for s, t in sorted(edges)]
    return {"nodes": list(nodes.values()), "edges": edge_list}


# --- PyQt6 worker: run build() off the GUI thread ------------------------
#
# PyQt6 is an optional dependency of this module -- only needed if you
# actually use GraphBuildWorker. CLI/headless usage of build() above works
# without PyQt6 installed at all.
try:
    from PyQt6.QtCore import QObject, QThread, pyqtSignal
except ImportError:  # pragma: no cover - PyQt6 not installed
    QObject = object  # type: ignore[assignment,misc]
    QThread = None  # type: ignore[assignment]
    pyqtSignal = None  # type: ignore[assignment]


if QThread is not None:

    class GraphBuildWorker(QObject):
        """
        Runs build() on a background QThread and reports progress/results
        back to the GUI thread via Qt signals, so the caller never blocks
        the Qt event loop while the crawl is in flight.

        Usage (from GUI/main thread):
            worker = GraphBuildWorker(seed_id, depth=3, per_node=5)
            thread = QThread()
            worker.moveToThread(thread)

            worker.progress.connect(on_progress)   # (level, nodes, edges) -> None
            worker.finished.connect(on_finished)   # (graph_dict) -> None
            worker.error.connect(on_error)          # (message: str) -> None

            thread.started.connect(worker.run)
            worker.finished.connect(thread.quit)
            worker.error.connect(thread.quit)

            thread.start()

        Signals are emitted from the worker thread; Qt automatically queues
        their delivery onto the thread that owns the connected slot (the
        GUI thread, if the slot belongs to a widget), so it's safe to update
        widgets directly inside on_progress/on_finished/on_error.
        """

        # (level, node_count_so_far, edge_count_so_far)
        progress = pyqtSignal(int, int, int)
        # (level, new_node_dicts, new_edge_dicts) -- the actual data added
        # this level, for a caller that wants to draw incrementally instead
        # of just tracking running totals via `progress`. See
        # LevelDataCallback's docstring for the exact dict shapes.
        levelData = pyqtSignal(int, list, list)
        # final {"nodes": [...], "edges": [...]}
        finished = pyqtSignal(dict)
        # error message, emitted instead of `finished` if build() raises
        error = pyqtSignal(str)

        def __init__(self, seed_id: str, depth: int, per_node: int,
                     parent: Optional[QObject] = None) -> None:
            super().__init__(parent)
            self.seed_id = seed_id
            self.depth = depth
            self.per_node = per_node

        def run(self) -> None:
            """Entry point connected to QThread.started. Runs on the worker thread."""
            try:
                graph = build(
                    self.seed_id, self.depth, self.per_node,
                    on_level_complete=lambda level, n, e: self.progress.emit(level, n, e),
                    on_level_data=lambda level, nodes, edges:
                        self.levelData.emit(level, nodes, edges),
                )
            except Exception as exc:  # noqa: BLE001 - surface any failure to the GUI
                self.error.emit(str(exc))
                return
            self.finished.emit(graph)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a citation graph via batched level expansion.")
    parser.add_argument("term", nargs="?", default="graph neural networks",
                         help="Search term or paper ID (ARXIV:..., DOI:..., etc.)")
    parser.add_argument("--depth", type=int, default=2,
                         help="Number of levels to expand beyond the seed (default: 2)")
    parser.add_argument("--per-node", type=int, default=5,
                         help="Max neighbors kept per node per direction (default: 5)")
    parser.add_argument("--out", default="graph.json",
                         help="Output JSON path (default: graph.json)")
    args = parser.parse_args()

    seed_id = resolve_seed(args.term)
    if not seed_id:
        print(f"No paper found for '{args.term}'")
        raise SystemExit(1)

    graph = build(seed_id, depth=args.depth, per_node=args.per_node)
    print(f"\nGraph: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2, ensure_ascii=False)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
