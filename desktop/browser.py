#!/usr/bin/env python3
"""Shadow Browser — a real Chromium browser (PyQt6 / QtWebEngine).

Launched as its own process by the desktop app so its Qt event loop doesn't
clash with pywebview's GTK loop. Full browsing: tabs, address bar, history,
back/forward/reload, persistent cookies, downloads — it *is* Chromium.

    python browser.py [url]
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

from PyQt6.QtCore import QUrl, Qt
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QLineEdit, QMainWindow, QTabWidget, QToolBar,
)
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PyQt6.QtWebEngineWidgets import QWebEngineView

PROFILE_DIR = str(Path.home() / ".shadow-desktop" / "browser")
HOME = "https://www.google.com"


def normalize(text: str) -> str:
    """Turn the address-bar text into a URL (or a Google search)."""
    t = (text or "").strip()
    if not t:
        return HOME
    if "://" in t:
        return t
    if " " not in t and "." in t:
        return "https://" + t
    return f"https://www.google.com/search?q={quote(t)}"


class Browser(QMainWindow):
    def __init__(self, start_url: str | None = None):
        super().__init__()
        self.setWindowTitle("Shadow Browser")
        self.resize(1200, 820)

        # One shared, persistent Chromium profile (cookies, cache, history).
        Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)
        self.profile = QWebEngineProfile("shadow", self)
        self.profile.setPersistentStoragePath(PROFILE_DIR)
        self.profile.setCachePath(PROFILE_DIR)
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._sync_bar)
        self.setCentralWidget(self.tabs)

        nav = QToolBar()
        nav.setMovable(False)
        self.addToolBar(nav)
        self._act(nav, "‹", "Back", lambda: self._cur() and self._cur().back(), "Alt+Left")
        self._act(nav, "›", "Forward", lambda: self._cur() and self._cur().forward(), "Alt+Right")
        self._act(nav, "⟳", "Reload", lambda: self._cur() and self._cur().reload(), "Ctrl+R")
        self._act(nav, "⌂", "Home", lambda: self._cur() and self._cur().setUrl(QUrl(HOME)), None)

        self.bar = QLineEdit()
        self.bar.setClearButtonEnabled(True)
        self.bar.setPlaceholderText("Search Google or type a URL")
        self.bar.returnPressed.connect(self._navigate)
        nav.addWidget(self.bar)

        self._act(nav, "＋", "New tab", lambda: self._add_tab(), "Ctrl+T")

        # Global shortcuts
        for seq, fn in (
            ("Ctrl+T", lambda: self._add_tab()),
            ("Ctrl+W", lambda: self._close_tab(self.tabs.currentIndex())),
            ("Ctrl+L", lambda: (self.bar.setFocus(), self.bar.selectAll())),
        ):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.triggered.connect(fn)
            self.addAction(a)

        self._add_tab(start_url or HOME)

    # -- helpers ----------------------------------------------------------- #
    def _act(self, bar, text, tip, fn, shortcut):
        a = QAction(text, self)
        a.setToolTip(tip)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(fn)
        bar.addAction(a)

    def _new_view(self) -> QWebEngineView:
        view = QWebEngineView()
        view.setPage(QWebEnginePage(self.profile, view))
        view.urlChanged.connect(lambda u, v=view: self._on_url(v, u))
        view.titleChanged.connect(lambda t, v=view: self._on_title(v, t))
        view.iconChanged.connect(lambda i, v=view: self._on_icon(v, i))
        return view

    def _add_tab(self, url: str | None = None):
        view = self._new_view()
        idx = self.tabs.addTab(view, "New Tab")
        self.tabs.setCurrentIndex(idx)
        view.setUrl(QUrl(normalize(url) if url else HOME))
        self.bar.setFocus()

    def _close_tab(self, idx: int):
        if self.tabs.count() <= 1:
            self.close()
            return
        w = self.tabs.widget(idx)
        self.tabs.removeTab(idx)
        if w:
            w.deleteLater()

    def _cur(self) -> QWebEngineView | None:
        return self.tabs.currentWidget()

    def _navigate(self):
        view = self._cur()
        if view:
            view.setUrl(QUrl(normalize(self.bar.text())))

    def _sync_bar(self):
        view = self._cur()
        if view:
            self.bar.setText(view.url().toString())

    def _on_url(self, view, url: QUrl):
        if view is self._cur():
            self.bar.setText(url.toString())
            self.bar.setCursorPosition(0)

    def _on_title(self, view, title: str):
        idx = self.tabs.indexOf(view)
        if idx >= 0:
            self.tabs.setTabText(idx, (title or "New Tab")[:24])
        if view is self._cur():
            self.setWindowTitle(f"{title} — Shadow Browser" if title else "Shadow Browser")

    def _on_icon(self, view, icon):
        idx = self.tabs.indexOf(view)
        if idx >= 0 and not icon.isNull():
            self.tabs.setTabIcon(idx, icon)


def main() -> int:
    start = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].strip() else None
    app = QApplication(sys.argv)
    app.setApplicationName("Shadow Browser")
    win = Browser(start)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
