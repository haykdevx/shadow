// static/js/browserLauncher.js
// Desktop-only: launches the bundled Chromium browser (PyQt6/QtWebEngine) in
// its own window via the pywebview bridge. In a normal web browser there is no
// pywebview API, so the sidebar item stays hidden and nothing changes.

function isDesktop() {
  return !!(window.pywebview && window.pywebview.api && window.pywebview.api.open_browser);
}

function reveal() {
  if (!isDesktop()) return;
  document.documentElement.classList.add('desktop-app');
  document.getElementById('tool-browser-btn')?.removeAttribute('hidden');
}

function open() {
  try {
    window.pywebview.api.open_browser('');
  } catch (_) { /* not in the desktop app */ }
}

function init() {
  reveal();
  // pywebview injects its bridge asynchronously; re-check when it's ready.
  window.addEventListener('pywebviewready', reveal);
  document.getElementById('tool-browser-btn')?.addEventListener('click', open);
  document.getElementById('rail-browser')?.addEventListener('click', open);
}

export default { init, open, isDesktop };
