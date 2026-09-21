"""
search_page.py -- the Search page: a search bar + an infinitely-scrolling
list of results, backed by search.relevance_search().

Kept simple: one QListWidget with a custom row widget per result, plus a
spinner row appended/removed at the bottom while a page is loading. All
network calls run on a background QThread so the UI never blocks.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import search as search_api

PAGE_SIZE = 15
# Trigger loading the next page once the scrollbar is within this many
# pixels of the bottom.
SCROLL_BOTTOM_THRESHOLD = 40


class SearchWorker(QObject):
    """
    Runs one relevance_search() call on a background thread. One instance
    is used per request (a fresh worker/thread pair per page load), matching
    the same off-GUI-thread pattern citation_graph.GraphBuildWorker uses for
    the graph builder.
    """

    finished = pyqtSignal(dict)  # raw relevance_search() response
    error = pyqtSignal(str)

    def __init__(self, query: str, offset: int, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.query = query
        self.offset = offset

    def run(self) -> None:
        try:
            result = search_api.relevance_search(
                self.query, limit=PAGE_SIZE, offset=self.offset
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            self.error.emit(str(exc))
            return
        self.finished.emit(result)


def _format_authors(authors: list[dict]) -> str:
    names = [a.get("name", "") for a in (authors or []) if a.get("name")]
    return ", ".join(names)


class ResultListWidget(QListWidget):
    """QListWidget that also treats Enter/Return on the current item as
    activation, matching double-click.

    Qt's QAbstractItemView only emits `activated` on Enter/Return on
    non-macOS platforms -- on macOS its keyPressEvent handler for
    Key_Enter/Key_Return only attempts inline editing and otherwise ignores
    the event, it never emits `activated` (see qabstractitemview.cpp's
    Q_OS_MACOS branch). So relying on itemActivated alone silently drops
    "press Enter to open" on macOS. Catching the key here and emitting
    itemActivated ourselves makes the behavior consistent across platforms.
    """

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override naming
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            item = self.currentItem()
            if item is not None:
                self.itemActivated.emit(item)
                event.accept()
                return
        super().keyPressEvent(event)


class ResultRow(QWidget):
    """One search result: title on top, 'year · authors' underneath. The
    author line is a single line that Qt elides (clips with '...') if it's
    wider than the row, rather than wrapping -- so as many authors as
    possible are shown before the clip.
    """

    def __init__(self, paper: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.paper = paper

        title = paper.get("title") or "(untitled)"
        year = paper.get("year")
        authors = _format_authors(paper.get("authors") or [])
        subtitle = " · ".join(part for part in (str(year) if year else "", authors) if part)

        title_label = QLabel(title)
        title_label.setWordWrap(True)
        title_label.setStyleSheet("font-weight: bold;")

        subtitle_label = QLabel()
        subtitle_label.setText(subtitle)
        # Single line, elided with '...' when it doesn't fit -- shows as
        # many authors as possible instead of wrapping to a second line.
        subtitle_label.setWordWrap(False)
        subtitle_label.setStyleSheet("color: #555;")
        self._subtitle_full_text = subtitle
        self._subtitle_label = subtitle_label

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)
        layout.addWidget(title_label)
        layout.addWidget(subtitle_label)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override naming
        super().resizeEvent(event)
        # Re-elide on resize since QLabel doesn't do this automatically for
        # plain (non-elided-by-default) single line text.
        metrics = self._subtitle_label.fontMetrics()
        elided = metrics.elidedText(
            self._subtitle_full_text,
            Qt.TextElideMode.ElideRight,
            self._subtitle_label.width(),
        )
        self._subtitle_label.setText(elided)


class SearchPage(QWidget):
    """
    Search bar + infinite-scroll result list.

    Emits `resultSelected(paper_dict)` when a result row is double-clicked
    or activated via Enter/Return (a plain single click only selects/
    highlights the row), so a parent window can switch to the Viewer page
    for that paper.
    """

    resultSelected = pyqtSignal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.query = ""
        self.offset = 0
        self.total = 0
        self._loading = False
        self._thread: QThread | None = None
        self._worker: SearchWorker | None = None

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search for papers...")
        self.search_input.returnPressed.connect(self._run_search)

        search_button = QPushButton("Search")
        search_button.clicked.connect(self._run_search)

        search_bar_layout = QHBoxLayout()
        search_bar_layout.addWidget(self.search_input)
        search_bar_layout.addWidget(search_button)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #a00;")
        self.status_label.setVisible(False)

        self.results_list = ResultListWidget()
        # itemActivated fires on double-click, and ResultListWidget's
        # keyPressEvent override additionally emits it on Enter/Return --
        # exactly "double-click or Enter", not a plain single click (which
        # only selects/highlights the row).
        self.results_list.itemActivated.connect(self._on_item_activated)
        self.results_list.verticalScrollBar().valueChanged.connect(
            self._on_scroll
        )

        self.spinner = QProgressBar()
        self.spinner.setRange(0, 0)  # indeterminate/busy mode, no extra assets
        self.spinner.setFixedHeight(16)
        self.spinner.setTextVisible(False)
        self.spinner.setVisible(False)

        layout = QVBoxLayout(self)
        layout.addLayout(search_bar_layout)
        layout.addWidget(self.status_label)
        layout.addWidget(self.results_list)
        layout.addWidget(self.spinner)

    # -- search flow ------------------------------------------------------

    def _run_search(self) -> None:
        query = self.search_input.text().strip()
        if not query or self._loading:
            return

        self.query = query
        self.offset = 0
        self.total = 0
        self.results_list.clear()
        self._hide_error()
        self._load_page()

    def _load_page(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.spinner.setVisible(True)

        self._thread = QThread()
        self._worker = SearchWorker(self.query, self.offset)
        self._worker.moveToThread(self._thread)
        self._worker.finished.connect(self._on_page_loaded)
        self._worker.error.connect(self._on_page_error)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        self._thread.start()

    def _on_page_loaded(self, result: dict) -> None:
        self._loading = False
        self.spinner.setVisible(False)

        self.total = result.get("total") or 0
        papers = result.get("data") or []
        for paper in papers:
            self._add_result_row(paper)
        self.offset += len(papers)

    def _on_page_error(self, message: str) -> None:
        self._loading = False
        self.spinner.setVisible(False)
        self._show_error(f"Search failed: {message}")

    def _add_result_row(self, paper: dict) -> None:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, paper)
        row = ResultRow(paper)
        item.setSizeHint(row.sizeHint())
        self.results_list.addItem(item)
        self.results_list.setItemWidget(item, row)

    def _show_error(self, message: str) -> None:
        self.status_label.setText(message)
        self.status_label.setVisible(True)

    def _hide_error(self) -> None:
        self.status_label.setVisible(False)

    # -- infinite scroll ----------------------------------------------------

    def _on_scroll(self, value: int) -> None:
        scrollbar = self.results_list.verticalScrollBar()
        near_bottom = value >= scrollbar.maximum() - SCROLL_BOTTOM_THRESHOLD
        has_more = self.offset < self.total
        if near_bottom and has_more and not self._loading and self.query:
            self._load_page()

    # -- result selection ---------------------------------------------------

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        paper = item.data(Qt.ItemDataRole.UserRole)
        if paper:
            self.resultSelected.emit(paper)


if __name__ == "__main__":
    import sys

    from PyQt6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    page = SearchPage()
    page.resultSelected.connect(
        lambda paper: print(f"Selected: {paper.get('title')}")
    )
    page.setWindowTitle("Search Page (standalone test)")
    page.resize(500, 600)
    page.show()
    sys.exit(app.exec())
