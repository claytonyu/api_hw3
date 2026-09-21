"""
main_window.py -- app entry point. Wires SearchPage and ViewerPage together
in a single QMainWindow via QStackedWidget, per VIEWER_PLAN.md Step 3.

Run:
  python3 main_window.py
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMainWindow, QStackedWidget

from search_page import SearchPage
from viewer_page import DetailDock, ViewerPage

SEARCH_PAGE_INDEX = 0
VIEWER_PAGE_INDEX = 1


class MainWindow(QMainWindow):
    """Window chrome + page-switching logic only -- no business logic
    beyond wiring signals between SearchPage and ViewerPage."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Citation Graph Viewer")
        self.resize(1100, 750)

        self.detail_dock = DetailDock(self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.detail_dock)

        self.search_page = SearchPage()
        self.viewer_page = ViewerPage(self.detail_dock)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.search_page)   # index 0
        self.stack.addWidget(self.viewer_page)   # index 1
        self.setCentralWidget(self.stack)

        self.search_page.resultSelected.connect(self._on_result_selected)
        self.viewer_page.backRequested.connect(self._on_back_requested)

    def _on_result_selected(self, paper: dict) -> None:
        self.stack.setCurrentIndex(VIEWER_PAGE_INDEX)
        self.viewer_page.load_paper(paper)

    def _on_back_requested(self) -> None:
        self.viewer_page.stop_and_reset()
        self.stack.setCurrentIndex(SEARCH_PAGE_INDEX)
        # Search page's list/query state is left as-is -- it wasn't torn
        # down, so it's still there if the user scrolls back to it.


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
