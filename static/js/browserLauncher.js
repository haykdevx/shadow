// static/js/browserLauncher.js
// Desktop-only: opens the embedded browser. In the Qt/QtWebEngine shell the
// browser is a tab in the SAME window (we ask the shell for a new tab by
// navigating to a sentinel the shell intercepts). In the older pywebview shell
// it falls back to the pywebview API. In a normal web browser there is no
// desktop runtime, so the sidebar item stays hidden and nothing changes.

function isDesktop() {
  return !!(window.shadowDesktop || (window.pywebview && window.pywebview.api));
}

function reveal() {
  if (!isDesktop()) return;
  document.documentElement.classList.add('desktop-app');
  document.getElementById('tool-browser-btn')?.removeAttribute('hidden');
}

function open() {
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_browser) {
      window.pywebview.api.open_browser('');           // legacy pywebview shell
    } else if (window.shadowDesktop) {
      // Qt shell: navigating to this sentinel host opens a new browser TAB
      // in the same window; the shell cancels the actual navigation.
      window.location.assign('https://newtab.shadow.invalid/');
    }
  } catch (_) { /* not in the desktop app */ }
}

function init() {
  reveal();
  window.addEventListener('pywebviewready', reveal);
  document.getElementById('tool-browser-btn')?.addEventListener('click', open);
  document.getElementById('rail-browser')?.addEventListener('click', open);
}

export default { init, open, isDesktop };
