"""
viewer_page.py -- the Viewer page: builds and displays a citation graph for
one seed paper.

Uses QGraphicsView/QGraphicsScene for the canvas (native to Qt, no extra
dependency). Nodes are drawn as circles labeled with an author + a
truncated title; edges are arrows pointing from a citing paper to the
paper it cites. The graph builds progressively as citation_graph.build()
streams in new levels via GraphBuildWorker (see Step 7 wiring below).

Clicking a node opens/updates a QDockWidget with the paper's full details.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QEvent, QLineF, QObject, QPointF, QRectF, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFormLayout,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from citation_graph import GraphBuildWorker
from graph_layout import LayoutCache

NODE_COLOR_SEED = QColor("#4a90d9")
NODE_COLOR_REFERENCE = QColor("#7fb069")
NODE_COLOR_CITATION = QColor("#d97a4a")
EDGE_COLOR = QColor("#888888")
ARROW_SIZE = 10
# Title truncation length scales with the circle's diameter, so larger
# (higher-citation) bubbles show more of the title instead of just a
# bigger rendering of the same short snippet. Calibrated so a 150px circle
# (the original fixed node size before per-node sizing was added) keeps the
# original 45-character length.
TITLE_CHARS_PER_DIAMETER = 45 / 150
MIN_TITLE_CHARS = 15

DEFAULT_DEPTH = 3
DEFAULT_PER_NODE = 5

# Node circle sizing, driven by citationCount (see node_diameter_for()):
#   - the seed paper is always drawn at SEED_NODE_DIAMETER, regardless of
#     its own citationCount.
#   - every other node's circle AREA (not diameter) is proportional to its
#     citationCount, calibrated so that a node with the same citationCount
#     as the seed would also compute to SEED_NODE_DIAMETER -- i.e. the seed
#     defines the area-per-citation constant `k` for the whole graph.
#   - the result is clamped to [MIN_NODE_DIAMETER, MAX_NODE_DIAMETER].
MIN_NODE_DIAMETER = 50
MAX_NODE_DIAMETER = 500
SEED_NODE_DIAMETER = 300

# Fixed padding kept around the graph's content inside the canvas, so nodes
# never sit flush against the scrollable edge / fit-in-view boundary.
# Hardcoded to one max-size node's diameter, per spec.
SCENE_PADDING = MAX_NODE_DIAMETER

# Trackpad pinch-to-zoom (QNativeGestureEvent) bounds -- how far in/out the
# graph can be scaled via ZoomNativeGesture.
MIN_ZOOM_SCALE = 0.2
MAX_ZOOM_SCALE = 5.0

# Detail dock field order: (label, paper dict key). Adding a future field
# (e.g. "Venue") is a one-line addition here -- see build_detail_fields().
FIELD_ORDER = [
    ("Authors", "authors"),
    ("Year", "year"),
    ("Citations", "citationCount"),
    ("References", "referenceCount"),
]

NODE_COLOR_BY_TRACK = {
    "seed": NODE_COLOR_SEED,
    "reference": NODE_COLOR_REFERENCE,
    "citation": NODE_COLOR_CITATION,
}


def _first_author_label(authors: list[dict]) -> str:
    names = [a.get("name", "") for a in (authors or []) if a.get("name")]
    if not names:
        return "(unknown)"
    if len(names) == 1:
        return names[0]
    # last name of the first author, or the whole name if it has no space
    first = names[0].split()[-1]
    return f"{first} et al."


def _truncate_title(title: str, max_chars: int) -> str:
    title = title or ""
    if len(title) <= max_chars:
        return title
    return title[: max_chars - 1].rstrip() + "\u2026"


def _title_max_chars_for(diameter: float) -> int:
    return max(MIN_TITLE_CHARS, int(round(diameter * TITLE_CHARS_PER_DIAMETER)))


# Node label font size scales with the circle's diameter so large,
# high-citation bubbles get correspondingly larger text instead of the same
# small fixed size everywhere. Roughly diameter/14, clamped to a sane range.
MIN_NODE_FONT_PT = 6
MAX_NODE_FONT_PT = 32
FONT_PT_PER_DIAMETER = 1 / 14

# Text is allowed to use most of the circle's width/height, but not the
# full diameter -- text near the top/bottom of a circle would poke outside
# the curved boundary, so height is capped to a fraction of the diameter
# to keep the label roughly inscribed.
NODE_TEXT_WIDTH_FRACTION = 0.85
NODE_TEXT_HEIGHT_FRACTION = 0.8


def _node_font_size_for(diameter: float) -> int:
    size = diameter * FONT_PT_PER_DIAMETER
    return int(round(max(MIN_NODE_FONT_PT, min(MAX_NODE_FONT_PT, size))))


def node_diameter_for(citation_count: int | None, seed_citation_count: int | None) -> float:
    """
    Diameter (px) for a node's circle, driven by citationCount.

    Area (pi * (d/2)^2) is proportional to citationCount, calibrated so
    that the seed paper's OWN citationCount would map to exactly
    SEED_NODE_DIAMETER (this defines the area-per-citation constant `k` for
    the whole graph) -- i.e.:

        k = area(SEED_NODE_DIAMETER) / seed_citation_count
        area(node) = k * citation_count(node)
        diameter(node) = 2 * sqrt(area(node) / pi)

    The seed itself is always drawn at SEED_NODE_DIAMETER exactly (handled
    by the caller, not here) regardless of what this function would compute
    for it.

    citationCount <= 0 (or missing) floors to MIN_NODE_DIAMETER. If the
    seed's own citationCount is <= 0/missing, `k` would be undefined
    (division by zero) -- in that edge case we fall back to treating the
    seed's citationCount as 1 purely for computing `k`, so every other
    node's size is still well-defined instead of raising or defaulting to
    all-minimum circles.
    """
    count = citation_count or 0
    if count <= 0:
        return MIN_NODE_DIAMETER

    seed_count = seed_citation_count or 0
    if seed_count <= 0:
        seed_count = 1  # fallback: see docstring

    seed_area = math.pi * (SEED_NODE_DIAMETER / 2) ** 2
    k = seed_area / seed_count
    area = k * count
    diameter = 2 * math.sqrt(area / math.pi)

    return max(MIN_NODE_DIAMETER, min(MAX_NODE_DIAMETER, diameter))


def build_detail_fields(paper: dict) -> list[tuple[str, str]]:
    """Returns (label, display_value) pairs for the detail dock, skipping
    fields that are missing. New fields can be added to FIELD_ORDER without
    touching this function, as long as the value is already a plain
    string/int/list-of-authors."""
    out = []
    for label, key in FIELD_ORDER:
        value = paper.get(key)
        if value in (None, "", []):
            continue
        if key == "authors":
            value = ", ".join(a.get("name", "?") for a in value)
        out.append((label, str(value)))
    return out


def paper_link(paper: dict) -> tuple[str, str] | None:
    """Pick the single best outbound link for a paper, returning
    (label, url) or None if nothing usable is available.

    Preference order:
      1. Open-access PDF (openAccessPdf.url), if present and non-empty --
         a direct, freely readable copy of the paper.
      2. DOI (externalIds.DOI), resolved via doi.org -- a stable, canonical
         link that works for most published papers.
      3. Semantic Scholar's own page for the paper (`url`), which always
         exists and is a reasonable fallback for preprints/venues without
         a DOI.
    """
    open_access = paper.get("openAccessPdf") or {}
    pdf_url = open_access.get("url")
    if pdf_url:
        return ("Open-access PDF", pdf_url)

    doi = (paper.get("externalIds") or {}).get("DOI")
    if doi:
        return ("DOI", f"https://doi.org/{doi}")

    s2_url = paper.get("url")
    if s2_url:
        return ("Semantic Scholar page", s2_url)

    return None


NODE_PEN_WIDTH = 1.5
NODE_PEN_WIDTH_SELECTED = 3.5


class PaperNodeItem(QGraphicsEllipseItem):
    """One paper, drawn as a filled circle with an author label and a
    truncated title. Clicking it (press AND release on this same item,
    without dragging past the platform's drag threshold in between)
    notifies the owning ViewerPage and selects the node, which bolds its
    outline."""

    def __init__(self, paper: dict, track: str, x: float, y: float,
                 on_clicked, diameter: float) -> None:
        super().__init__(-diameter / 2, -diameter / 2, diameter, diameter)
        self.paper = paper
        self.track = track
        self.diameter = diameter
        self._on_clicked = on_clicked
        self._base_color = NODE_COLOR_BY_TRACK.get(track, NODE_COLOR_REFERENCE)
        self._press_scene_pos: QPointF | None = None

        self.setBrush(QBrush(self._base_color))
        self.setPen(QPen(QColor("#333333"), NODE_PEN_WIDTH))
        self.setPos(QPointF(x, y))
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setZValue(1)  # nodes drawn above edges
        # Enables Qt's own selection bookkeeping (isSelected()/setSelected())
        # so only one node is "selected" at a time within the scene, and so
        # itemChange() below is notified on selection changes.
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)

        self._text_item = self.scene_text_item(paper, diameter)

    def scene_text_item(self, paper: dict, diameter: float):
        """Builds the node's label, sized to fill the circle: font size
        scales with diameter, and the author+title text is used if it fits
        vertically at that size -- otherwise it falls back to just the
        author label (elided by width) so small circles don't overflow."""
        from PyQt6.QtWidgets import QGraphicsTextItem

        author_label = _first_author_label(paper.get("authors") or [])
        title_label = _truncate_title(paper.get("title") or "", _title_max_chars_for(diameter))

        font = QFont()
        font.setPointSize(_node_font_size_for(diameter))

        text_width = max(20.0, diameter * NODE_TEXT_WIDTH_FRACTION)
        max_text_height = diameter * NODE_TEXT_HEIGHT_FRACTION

        item = QGraphicsTextItem(self)
        item.setFont(font)
        item.setTextWidth(text_width)
        item.setDefaultTextColor(QColor("#111111"))

        full_text = f"{author_label}\n{title_label}" if title_label else author_label
        item.setPlainText(full_text)
        if item.boundingRect().height() > max_text_height:
            # Author+title doesn't fit vertically at this font size -- fall
            # back to author-only, elided by width if it's still too wide
            # to fit on one line (rather than letting it wrap and possibly
            # still overflow).
            metrics = QFontMetrics(font)
            elided_author = metrics.elidedText(
                author_label, Qt.TextElideMode.ElideRight, int(text_width)
            )
            item.setPlainText(elided_author)

        # Center the text block inside the circle.
        rect = item.boundingRect()
        item.setPos(-rect.width() / 2, -rect.height() / 2)
        return item

    def itemChange(self, change, value):  # noqa: N802 - Qt override naming
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedChange:
            width = NODE_PEN_WIDTH_SELECTED if value else NODE_PEN_WIDTH
            pen = self.pen()
            pen.setWidthF(width)
            self.setPen(pen)
        return super().itemChange(change, value)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override naming
        # Record where the press happened, but deliberately DON'T call
        # super().mousePressEvent() here: QGraphicsItem's default press
        # handling selects an ItemIsSelectable item immediately on press,
        # which is exactly the "selected on the way to becoming a drag"
        # behavior we don't want. Selection is instead applied ourselves in
        # mouseReleaseEvent, only once we know press+release landed on the
        # same item without dragging in between. event.accept() still needs
        # to be called so this item keeps receiving the matching move/release
        # events for this press (Qt routes those to whichever item accepted
        # the press).
        self._press_scene_pos = event.scenePos()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override naming
        press_pos = self._press_scene_pos
        self._press_scene_pos = None
        event.accept()
        if press_pos is None:
            return

        # Qt's own platform-standard tolerance for "did the mouse actually
        # drag, or just jitter slightly during a click" -- the same value
        # QDrag/QGraphicsView use internally to decide when a press becomes
        # a drag rather than a click.
        threshold = QApplication.startDragDistance()
        moved = QLineF(press_pos, event.scenePos()).length()
        released_on_self = self.contains(self.mapFromScene(event.scenePos()))

        if moved <= threshold and released_on_self:
            self.scene().clearSelection()
            self.setSelected(True)
            if self._on_clicked:
                self._on_clicked(self.paper)


class EdgeItem(QGraphicsPathItem):
    """A directed edge from `source_item` to `target_item`, drawn as a line
    from the source circle's boundary to the target circle's boundary (not
    center-to-center, so the line doesn't disappear under the nodes), with
    an arrowhead at the target end."""

    def __init__(self, source_item: PaperNodeItem, target_item: PaperNodeItem) -> None:
        super().__init__()
        self.source_item = source_item
        self.target_item = target_item
        self.setPen(QPen(EDGE_COLOR, 1.5))
        self.setBrush(QBrush(EDGE_COLOR))
        self.setZValue(0)  # edges drawn below nodes
        self.update_path()

    def update_path(self) -> None:
        source_radius = self.source_item.diameter / 2
        target_radius = self.target_item.diameter / 2
        center_line = QLineF(self.source_item.pos(), self.target_item.pos())
        if center_line.length() == 0:
            return

        # Move each endpoint in from its own center to that node's own
        # circle boundary -- source and target can now be different sizes.
        unit = QPointF(center_line.dx(), center_line.dy()) / center_line.length()
        start = self.source_item.pos() + unit * source_radius
        end = self.target_item.pos() - unit * target_radius

        path = QPainterPath(start)
        path.lineTo(end)

        # Arrowhead at the target end, pointing along the line direction.
        angle = QLineF(start, end).angle()
        rad_left = math.radians(angle + 150)
        rad_right = math.radians(angle - 150)
        p1 = end + QPointF(math.cos(rad_left), -math.sin(rad_left)) * ARROW_SIZE
        p2 = end + QPointF(math.cos(rad_right), -math.sin(rad_right)) * ARROW_SIZE
        arrow_head = QPolygonF([end, p1, p2])
        path.addPolygon(arrow_head)

        self.setPath(path)


class GraphView(QGraphicsView):
    """QGraphicsView with trackpad pinch-to-zoom support.

    Qt reports a two-finger trackpad pinch as a stream of QNativeGestureEvent
    (gestureType() == Qt.NativeGestureType.ZoomNativeGesture) rather than a
    QWheelEvent. Each event's value() is an incremental scale delta -- the
    intended new scale is current_scale * (1 + value()) -- and the event's
    position() is where the two fingers are anchored on the trackpad,
    mapped to widget-local coordinates. Zooming is anchored there (like
    Preview/Maps) rather than at the viewport center.
    """

    def __init__(self, scene: QGraphicsScene, parent: QWidget | None = None) -> None:
        super().__init__(scene, parent)
        self._current_scale = 1.0
        # We compute our own zoom anchor point from the gesture's position
        # rather than relying on AnchorUnderMouse (trackpad gestures don't
        # move a real cursor the same way a mouse does on every platform).
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)

    def viewportEvent(self, event) -> bool:  # noqa: N802 - Qt override naming
        if event.type() == QEvent.Type.NativeGesture:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                self._handle_pinch_zoom(event)
                return True
        return super().viewportEvent(event)

    def _handle_pinch_zoom(self, event) -> None:
        new_scale = self._current_scale * (1.0 + event.value())
        new_scale = max(MIN_ZOOM_SCALE, min(MAX_ZOOM_SCALE, new_scale))
        factor = new_scale / self._current_scale
        if factor == 1.0:
            return

        # Anchor the zoom on the gesture's position: keep the scene point
        # currently under that position fixed on screen while scaling.
        anchor_view_pos = event.position()
        anchor_scene_pos = self.mapToScene(anchor_view_pos.toPoint())

        self.scale(factor, factor)
        self._current_scale = new_scale

        new_view_pos = self.mapFromScene(anchor_scene_pos)
        delta = new_view_pos - anchor_view_pos.toPoint()
        h_bar = self.horizontalScrollBar()
        v_bar = self.verticalScrollBar()
        h_bar.setValue(h_bar.value() + delta.x())
        v_bar.setValue(v_bar.value() + delta.y())


class ViewerPage(QWidget):
    """
    Graph canvas page. Call `load_paper(paper_dict)` to start building and
    displaying a citation graph for that paper.

    Signals:
      backRequested -- emitted when the user clicks "Back to Search".
    """

    backRequested = pyqtSignal()

    def __init__(self, detail_dock: "DetailDock", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.detail_dock = detail_dock

        self._layout_cache = LayoutCache()
        self._node_items: dict[str, PaperNodeItem] = {}
        self._edge_items: set[tuple[str, str]] = set()
        # citationCount of the current graph's seed paper -- defines the
        # area-per-citation constant used to size every other node (see
        # node_diameter_for()). Set once per load_paper() call, from the
        # seed node in level 0's on_level_data callback.
        self._seed_citation_count: int | None = None
        self._thread: QThread | None = None
        self._worker: GraphBuildWorker | None = None
        # Threads from a build() that's still running when the user leaves
        # this page (e.g. clicks "Back to Search"). Kept alive here only so
        # Qt doesn't complain about a QThread being destroyed while still
        # running; see stop_and_reset(). Removed once each finishes on its
        # own -- never waited on from the GUI thread.
        self._orphaned_threads: set[QThread] = set()

        back_button = QPushButton("\u2190 Back to Search")
        back_button.clicked.connect(self.backRequested.emit)

        self.status_label = QLabel("")
        self.spinner = QProgressBar()
        self.spinner.setRange(0, 0)
        self.spinner.setFixedWidth(120)
        self.spinner.setFixedHeight(16)
        self.spinner.setTextVisible(False)
        self.spinner.setVisible(False)

        top_bar = QHBoxLayout()
        top_bar.addWidget(back_button)
        top_bar.addWidget(self.status_label)
        top_bar.addStretch()
        top_bar.addWidget(self.spinner)

        self.scene = QGraphicsScene()
        self.view = GraphView(self.scene)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.view.setRenderHint(self.view.renderHints().Antialiasing)

        layout = QVBoxLayout(self)
        layout.addLayout(top_bar)
        layout.addWidget(self.view)

    # -- public API -----------------------------------------------------

    def load_paper(self, paper: dict) -> None:
        """Reset any previous graph and start building a new one for `paper`."""
        self.stop_and_reset()

        seed_id = paper.get("paperId")
        if not seed_id:
            self.status_label.setText("Error: selected paper has no id")
            return

        self.status_label.setText(f"Building graph for: {paper.get('title', seed_id)}")
        self.spinner.setVisible(True)

        self._thread = QThread()
        self._worker = GraphBuildWorker(seed_id, depth=DEFAULT_DEPTH,
                                         per_node=DEFAULT_PER_NODE)
        self._worker.moveToThread(self._thread)
        self._worker.levelData.connect(self._on_level_data)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.start()

    def stop_and_reset(self) -> None:
        """Clear the graph and stop reacting to any in-flight build.

        This does NOT cancel in-flight HTTP calls (build() has no
        cancellation check) -- it disconnects this page from the worker's
        signals and drops its reference to the thread/worker pair, so any
        results that arrive after this point are simply never drawn. See
        VIEWER_PLAN.md Step 7 for why full cancellation is out of scope.
        """
        if self._worker is not None:
            try:
                self._worker.levelData.disconnect(self._on_level_data)
                self._worker.progress.disconnect(self._on_progress)
                self._worker.finished.disconnect(self._on_finished)
                self._worker.error.disconnect(self._on_error)
            except TypeError:
                pass  # already disconnected

        # IMPORTANT: don't wait()/block here. The worker's run() is a single
        # blocking build() call, not an event loop -- QThread.quit() has no
        # effect on it, and wait() would block the GUI thread until the
        # whole crawl finishes on its own, which is exactly the freeze this
        # app is designed to avoid (see VIEWER_PLAN.md Step 7: build() has
        # no cancellation check, so "stop" only means "stop reacting").
        #
        # We still must not drop the last Python reference to a QThread
        # that may still be running (Qt logs "Destroyed while thread is
        # still running" and can misbehave on interpreter exit), so instead
        # of clearing self._thread/self._worker, we hand them off to a
        # class-level set that's cleaned up once they finish naturally via
        # their own finished/error -> thread.quit() connections (already
        # wired in load_paper()). This page no longer listens to them
        # (disconnected above), so no further UI updates occur.
        if self._thread is not None:
            self._orphaned_threads.add(self._thread)
            self._thread.finished.connect(
                lambda t=self._thread: self._orphaned_threads.discard(t)
            )
        self._thread = None
        self._worker = None

        self.scene.clear()
        self.scene.setSceneRect(QRectF())  # let Qt recompute from content again
        self._node_items.clear()
        self._edge_items.clear()
        self._layout_cache = LayoutCache()
        self._seed_citation_count = None
        self.view.resetTransform()
        self.view._current_scale = 1.0
        self.spinner.setVisible(False)
        self.status_label.setText("")

    # -- worker signal handlers ------------------------------------------

    def _on_level_data(self, level: int, new_nodes: list, new_edges: list) -> None:
        # Level 0 is always exactly [seed_node]. Record its citationCount
        # first -- it defines the area-per-citation constant every other
        # node's size is calibrated against (see node_diameter_for()).
        if level == 0 and new_nodes:
            self._seed_citation_count = new_nodes[0].get("citationCount")

        # Every node in new_nodes at this level shares the same (level, track)
        # bucket count needed by LayoutCache -- group them so the ring layout
        # spreads them out correctly.
        by_track: dict[str, list[dict]] = {}
        for node in new_nodes:
            by_track.setdefault(node.get("_track", "reference"), []).append(node)

        for track, nodes_in_track in by_track.items():
            count = len(nodes_in_track)
            for node in nodes_in_track:
                x, y = self._layout_cache.place(node["paperId"], level, track, count)
                self._add_node_item(node, track, x, y)

        for edge in new_edges:
            self._add_edge_item(edge["source"], edge["target"])

    def _add_node_item(self, paper: dict, track: str, x: float, y: float) -> None:
        if paper["paperId"] in self._node_items:
            return
        if track == "seed":
            diameter = SEED_NODE_DIAMETER
        else:
            diameter = node_diameter_for(paper.get("citationCount"),
                                          self._seed_citation_count)
        item = PaperNodeItem(paper, track, x, y, on_clicked=self._on_node_clicked,
                              diameter=diameter)
        self.scene.addItem(item)
        self._node_items[paper["paperId"]] = item
        self._update_scene_rect()

    def _update_scene_rect(self) -> None:
        # Pad the scene's rect (not the window/widget) so panning/fitting
        # always leaves room around the outermost nodes -- otherwise they'd
        # sit flush against the scrollable edge. itemsBoundingRect() already
        # covers node circles + edges + labels; grow it by a fixed margin
        # (one node's diameter) on every side.
        content_rect = self.scene.itemsBoundingRect()
        if content_rect.isNull():
            return
        padded_rect = content_rect.adjusted(
            -SCENE_PADDING, -SCENE_PADDING, SCENE_PADDING, SCENE_PADDING
        )
        self.scene.setSceneRect(padded_rect)

    def _add_edge_item(self, source_id: str, target_id: str) -> None:
        key = (source_id, target_id)
        if key in self._edge_items:
            return
        source_item = self._node_items.get(source_id)
        target_item = self._node_items.get(target_id)
        if not source_item or not target_item:
            return  # shouldn't happen given on_level_data's ordering, but be safe
        edge_item = EdgeItem(source_item, target_item)
        self.scene.addItem(edge_item)
        self._edge_items.add(key)

    def _on_progress(self, level: int, nodes: int, edges: int) -> None:
        self.status_label.setText(
            f"Building graph... level {level}, {nodes} papers, {edges} citations"
        )

    def _on_finished(self, graph: dict) -> None:
        self.spinner.setVisible(False)
        self.status_label.setText(
            f"Done: {len(graph['nodes'])} papers, {len(graph['edges'])} citations"
        )

    def _on_error(self, message: str) -> None:
        self.spinner.setVisible(False)
        self.status_label.setText(f"Error building graph: {message}")

    def _on_node_clicked(self, paper: dict) -> None:
        self.detail_dock.show_paper(paper)


class DetailDock(QDockWidget):
    """Side panel showing full details for a clicked paper. Title is shown
    as its own bold label, distinct from the rest of the fields (per spec);
    everything else comes from FIELD_ORDER via build_detail_fields()."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Paper Details", parent)
        self.setVisible(False)

        container = QWidget()
        outer_layout = QVBoxLayout(container)

        self.title_label = QLabel("")
        self.title_label.setWordWrap(True)
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        self.title_label.setFont(title_font)

        self.link_label = QLabel("")
        self.link_label.setWordWrap(True)
        self.link_label.setTextFormat(Qt.TextFormat.RichText)
        self.link_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.link_label.setOpenExternalLinks(True)
        self.link_label.setVisible(False)

        self.form_widget = QWidget()
        self.form_layout = QFormLayout(self.form_widget)

        outer_layout.addWidget(self.title_label)
        outer_layout.addWidget(self.link_label)
        outer_layout.addWidget(self.form_widget)
        outer_layout.addStretch()

        self.setWidget(container)

    def show_paper(self, paper: dict) -> None:
        self.title_label.setText(paper.get("title") or "(untitled)")

        link = paper_link(paper)
        if link:
            label, url = link
            self.link_label.setText(f'<a href="{url}">{label} \u2197</a>')
            self.link_label.setVisible(True)
        else:
            self.link_label.setVisible(False)

        while self.form_layout.rowCount():
            self.form_layout.removeRow(0)

        for label, value in build_detail_fields(paper):
            value_label = QLabel(value)
            value_label.setWordWrap(True)
            self.form_layout.addRow(f"{label}:", value_label)

        self.setVisible(True)
        self.raise_()


if __name__ == "__main__":
    import sys

    from PyQt6.QtWidgets import QApplication, QMainWindow

    app = QApplication(sys.argv)
    window = QMainWindow()

    dock = DetailDock(window)
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    page = ViewerPage(dock)
    page.backRequested.connect(lambda: print("Back to search requested"))
    window.setCentralWidget(page)
    window.resize(1000, 700)
    window.setWindowTitle("Viewer Page (standalone test)")
    window.show()

    # Hardcoded test seed, bypassing Search, per the plan's run order.
    page.load_paper({"paperId": "ARXIV:1706.03762", "title": "Attention is All you Need"})

    sys.exit(app.exec())
