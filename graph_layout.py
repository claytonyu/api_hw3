"""
graph_layout.py -- simple, fixed node positions for the citation graph viewer.

No third-party graph/viz library is used here (networkx is not installed in
this project's venv, and adding it wasn't requested), so positions are
computed with a small hand-written function based on the graph's own
level/track structure instead of a physics-based layout.

Placement rules:
  * The seed paper sits at the origin.
  * Reference-track nodes (papers the seed/its ancestors cite) are placed in
    rings on one half of the plane (angles 0..pi), one ring per level.
  * Citation-track nodes (papers that cite the seed/its descendants) are
    placed in rings on the other half (angles pi..2*pi), one ring per level.
  * Nodes within the same level+track are spread evenly across their half
    of the ring, so the two tracks never overlap.

This is intentionally simple and static: position_for() is meant to be
called exactly ONCE per node, at the moment that node is first added to the
graph. The result should be cached by the caller (see ViewerPage) and never
recomputed -- per-node positions are fixed for this version of the app.
"""

from __future__ import annotations

import math

# Pixel distance between successive levels' rings. Sized to comfortably fit
# two full-size nodes back-to-back (MAX_NODE_DIAMETER in viewer_page.py is
# 500px) plus some breathing room, since node circles now vary in size
# (citationCount-driven) instead of all being a fixed 150px -- a smaller
# spacing would let large-citation nodes on adjacent rings overlap.
RING_SPACING = 550

# "reference" nodes get angles in [0, pi] (positive y); "citation" nodes get
# angles in [pi, 2*pi] (negative y). Qt's QGraphicsScene y-axis increases
# DOWNWARD, so on screen this renders reference-track nodes BELOW the seed
# and citation-track nodes ABOVE it. Either way, the two tracks land in
# clearly separated halves -- which half is "up" vs "down" on screen is a
# rendering detail, not something this module needs to get right, since the
# important property is separation, not a specific up/down assignment.
TRACK_BASE_ANGLE = {
    "reference": 0.0,
    "citation": math.pi,
}
HALF_SPAN = math.pi  # each track gets 180 degrees of the ring


def position_for(level: int, index_in_level: int, count_in_level: int,
                  track: str) -> tuple[float, float]:
    """
    Return an (x, y) position for one node.

    Args:
      level            0 for the seed, 1 for its direct neighbors, etc.
      index_in_level   this node's position among others sharing the same
                       (level, track), e.g. 0..count_in_level-1
      count_in_level   how many nodes share this (level, track) pair
      track            "reference" or "citation" (ignored for level 0)

    Returns:
      (x, y) in scene coordinates, seed at (0, 0).
    """
    if level <= 0:
        return (0.0, 0.0)

    radius = level * RING_SPACING
    base_angle = TRACK_BASE_ANGLE.get(track, 0.0)

    # Nodes are spread strictly inside their half-ring (never exactly at the
    # 0/pi/2*pi boundary shared with the other track), so reference-track and
    # citation-track nodes at the same level never land on the same point.
    if count_in_level <= 1:
        angle = base_angle + HALF_SPAN / 2
    else:
        step = HALF_SPAN / (count_in_level + 1)
        angle = base_angle + step * (index_in_level + 1)

    x = radius * math.cos(angle)
    y = radius * math.sin(angle)
    return (x, y)


class LayoutCache:
    """
    Tracks how many nodes have been placed at each (level, track) so far,
    and assigns each new node a stable position exactly once.

    Usage: call `place(paper_id, level, track, count_in_level)` once per
    new node, in the order they're discovered within a level. Re-calling
    with a `paper_id` that's already placed returns its existing position
    unchanged (nodes never move once placed).
    """

    def __init__(self) -> None:
        self._positions: dict[str, tuple[float, float]] = {}
        self._next_index: dict[tuple[int, str], int] = {}

    def place(self, paper_id: str, level: int, track: str,
              count_in_level: int) -> tuple[float, float]:
        if paper_id in self._positions:
            return self._positions[paper_id]

        key = (level, track)
        index = self._next_index.get(key, 0)
        self._next_index[key] = index + 1

        pos = position_for(level, index, count_in_level, track)
        self._positions[paper_id] = pos
        return pos

    def get(self, paper_id: str) -> tuple[float, float] | None:
        return self._positions.get(paper_id)

    def __contains__(self, paper_id: str) -> bool:
        return paper_id in self._positions
