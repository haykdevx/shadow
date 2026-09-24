#!/usr/bin/env python3
"""Shadow desktop shell — a single Chromium (PyQt6/QtWebEngine) window that
hosts the Shadow app AND an embedded, fully-working browser as extra tabs.

Tab 0 is the Shadow app. Clicking "Browser" in the app sidebar (or the +)
opens a real Chromium browser tab in the SAME window — nothing pops out.

Started from launcher.run_gui; reuses launcher for Docker orchestration.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

from PyQt6.QtCore import QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QLineEdit, QMainWindow, QTabWidget, QToolBar,
)
from PyQt6.QtWebEngineCore import (
    QWebEnginePage, QWebEngineProfile, QWebEngineScript,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

import launcher  # Docker orchestration + splash/error HTML + config

APP_ID = "io.github.haykdevx.shadow"
APP_URL = launcher.APP_URL
PROFILE_DIR = str(Path.home() / ".shadow-desktop" / "webview")
NEWTAB_HOST = "newtab.shadow.invalid"  # sentinel the app navigates to for a new tab
SEARCH = "https://www.google.com"


def _normalize(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return SEARCH
    if "://" in t:
        return t
    if " " not in t and "." in t:
        return "https://" + t
    return f"https://www.google.com/search?q={quote(t)}"


class _Starter(QThread):
    """Brings the Docker stack up off the UI thread."""
    done = pyqtSignal(bool)

    def __init__(self, base):
        super().__init__()
        self.base = base

    def run(self):
        try:
            # REMOTE_MODE (SHADOW_DESKTOP_URL set): connecting to an existing
            # server, e.g. the user's own VPS deployment — no local Docker
            # engine required or touched, just wait for that server to answer.
            if not launcher.REMOTE_MODE:
                launcher.ensure_env()
                launcher.start_stack(self.base)
            self.done.emit(launcher.wait_for_health(launcher.START_TIMEOUT))
        except Exception as exc:  # noqa: BLE001
            launcher.log(f"startup failed: {exc}")
            self.done.emit(False)


class _Page(QWebEnginePage):
    """Routes new-window requests and the app's sentinel into in-window tabs."""

    def __init__(self, profile, shell):
        super().__init__(profile, shell)
        self._shell = shell

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if url.host() == NEWTAB_HOST:
            self._shell.add_browser_tab()
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    def createWindow(self, _type):  # target=_blank / window.open() -> new tab
        view = self._shell.add_browser_tab()
        return view.page()


class Shell(QMainWindow):
    def __init__(self, base):
        super().__init__()
        self.setWindowTitle("Shadow")
        self.resize(1280, 860)
        icon = Path(__file__).resolve().parent / "assets" / "icon-256.png"
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        # Persistent Chromium profile (cookies/cache/history) shared by all tabs.
        Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)
        self.profile = QWebEngineProfile("shadow", self)
        self.profile.setPersistentStoragePath(PROFILE_DIR)
        self.profile.setCachePath(PROFILE_DIR)
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        # Tell the page it's running inside the desktop shell (CSP-safe user
        # script): reveals the sidebar Browser item + desktop perf CSS.
        flag = QWebEngineScript()
        flag.setName("shadow-desktop-flag")
        flag.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        flag.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        flag.setRunsOnSubFrames(False)
        flag.setSourceCode("window.shadowDesktop = true;")
        self.profile.scripts().insert(flag)

        # Browser chrome (only shown on browser tabs).
        self.nav = QToolBar()
        self.nav.setMovable(False)
        self.addToolBar(self.nav)
        self._tb("‹", lambda: self._cur() and self._cur().back(), "Alt+Left")
        self._tb("›", lambda: self._cur() and self._cur().forward(), "Alt+Right")
        self._tb("⟳", lambda: self._cur() and self._cur().reload(), "Ctrl+R")
        self.bar = QLineEdit()
        self.bar.setPlaceholderText("Search Google or type a URL")
        self.bar.returnPressed.connect(self._navigate)
        self.nav.addWidget(self.bar)
        self.nav.hide()

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab)
        self.setCentralWidget(self.tabs)

        newtab = QAction("＋", self)
        newtab.setToolTip("New browser tab")
        newtab.triggered.connect(lambda: self.add_browser_tab())
        self.tabs.setCornerWidget(self._corner(newtab))
        for seq, fn in (("Ctrl+K", self._open_palette),
                        ("Ctrl+T", lambda: self.add_browser_tab()),
                        ("Ctrl+W", lambda: self._close_tab(self.tabs.currentIndex())),
                        ("Ctrl+L", lambda: (self.bar.setFocus(), self.bar.selectAll()))):
            a = QAction(self); a.setShortcut(QKeySequence(seq)); a.triggered.connect(fn)
            self.addAction(a)

        # Tab 0: the Shadow app. Splash now; real URL once healthy.
        self.app_view = self._new_view(app_tab=True)
        self.tabs.addTab(self.app_view, "Shadow")
        self.tabs.tabBar().setTabButton(0, self.tabs.tabBar().ButtonPosition.RightSide, None)
        self.app_view.setHtml(launcher.SPLASH_HTML, QUrl(APP_URL))
        self._update_chrome()

        self._starter = _Starter(base)
        self._starter.done.connect(self._on_ready)
        self._starter.start()

    def _open_palette(self):
        self.tabs.setCurrentWidget(self.app_view)
        self.app_view.setFocus()
        self.app_view.page().runJavaScript(
            "if (window.shadowCommandConsole) window.shadowCommandConsole.openPalette();"
        )

    # -- chrome helpers ---------------------------------------------------- #
    def _tb(self, text, fn, shortcut):
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.triggered.connect(fn)
        self.nav.addAction(a)

    def _corner(self, action):
        from PyQt6.QtWidgets import QToolButton
        b = QToolButton(); b.setDefaultAction(action); b.setAutoRaise(True)
        return b

    def _new_view(self, app_tab=False) -> QWebEngineView:
        view = QWebEngineView()
        view.setPage(_Page(self.profile, self))
        view._is_app = app_tab
        view.urlChanged.connect(lambda u, v=view: self._sync(v))
        view.titleChanged.connect(lambda t, v=view: self._title(v, t))
        view.iconChanged.connect(lambda i, v=view: self._icon(v, i))
        return view

    def add_browser_tab(self, url: str | None = None) -> QWebEngineView:
        view = self._new_view()
        idx = self.tabs.addTab(view, "New Tab")
        self.tabs.setCurrentIndex(idx)
        view.setUrl(QUrl(_normalize(url) if url else SEARCH))
        self.bar.setFocus()
        return view

    def _close_tab(self, idx: int):
        view = self.tabs.widget(idx)
        if view is None or view is self.app_view:   # never close the app tab
            return
        self.tabs.removeTab(idx)
        view.deleteLater()
        self._update_chrome()

    def _cur(self) -> QWebEngineView | None:
        return self.tabs.currentWidget()

    def _navigate(self):
        v = self._cur()
        if v and not getattr(v, "_is_app", False):
            v.setUrl(QUrl(_normalize(self.bar.text())))

    def _on_tab(self, _idx):
        self._update_chrome()
        self._sync(self._cur())

    def _update_chrome(self):
        v = self._cur()
        is_browser = bool(v) and not getattr(v, "_is_app", False)
        self.nav.setVisible(is_browser)
        self.tabs.tabBar().setVisible(self.tabs.count() > 1)

    def _sync(self, view):
        if view is not None and view is self._cur() and not getattr(view, "_is_app", False):
            self.bar.setText(view.url().toString())
            self.bar.setCursorPosition(0)

    def _title(self, view, title):
        idx = self.tabs.indexOf(view)
        if idx >= 0 and not getattr(view, "_is_app", False):
            self.tabs.setTabText(idx, (title or "New Tab")[:22])

    def _icon(self, view, icon):
        idx = self.tabs.indexOf(view)
        if idx >= 0 and not icon.isNull():
            self.tabs.setTabIcon(idx, icon)

    def _on_ready(self, ok: bool):
        if ok:
            self.app_view.setUrl(QUrl(APP_URL))
        else:
            hint = (
                f"Could not reach {APP_URL} within {launcher.START_TIMEOUT}s.\n"
                "Check your internet connection and that the server is running."
                if launcher.REMOTE_MODE else
                f"Shadow did not become healthy within {launcher.START_TIMEOUT}s.\n"
                "Check container logs:  docker compose logs shadow"
            )
            self.app_view.setHtml(launcher.error_html(hint), QUrl(APP_URL))


def run(base) -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Shadow")
    app.setDesktopFileName(APP_ID)   # Wayland/X11 icon + name matching
    win = Shell(base)
    win.show()
    rc = app.exec()
    if launcher.STOP_ON_EXIT and not launcher.REMOTE_MODE:
        launcher.stop_stack(base)
    return rc
