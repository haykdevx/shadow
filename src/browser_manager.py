"""Agent-driven Chromium browser, isolated per Shadow account.

Playwright drives one persistent Chromium context per owner so logins survive
across a task, while no two accounts ever share cookies or storage. All
irreversible-looking actions (payments, sends, posts, deletes, password entry,
arbitrary JS) stop at a server-side pending approval, mirroring the
`src/shadow_pc.py` confirmation contract: the agent can REQUEST, only a human
with a real session can CONFIRM.

Security posture:
- profiles live under `data/browser/profiles/<owner>` with mode 0700;
- non-http(s) schemes and private/loopback targets are refused unless
  explicitly allowed (`SHADOW_BROWSER_ALLOW_PRIVATE=1`), with the same check
  re-applied after redirects and on subresource requests by hostname;
- typed values are never logged and never written to disk; history and
  pending payloads show `***` for fill values;
- per-owner action rate limit and hard Playwright timeouts bound runaways.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import secrets
import shutil
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class BrowserError(RuntimeError):
    """A clean, user-facing browser failure."""


class BrowserConfirmContextError(BrowserError):
    """The page navigated between gating and approval; approval was voided."""


DATA_ROOT = Path(os.getenv("SHADOW_BROWSER_DATA", "data/browser"))
VIEWPORT = {"width": 1280, "height": 800}
ACTIONS = {
    "navigate", "read", "links", "click", "fill", "press", "eval",
    "screenshot", "back", "forward", "reload", "wait", "download",
    "tabs", "switch_tab", "status",
}
# Actions that never mutate page state and therefore never need approval.
READ_ACTIONS = {"read", "links", "screenshot", "tabs", "status", "wait"}

_RISKY_WORDS = re.compile(
    r"\b(buy|purchase|order|checkout|pay|payment|donate|subscribe|transfer|"
    r"send|submit|post|publish|tweet|share|reply|comment|delete|remove|"
    r"deactivate|unsubscribe|confirm|apply|book|reserve|sign\s*up|register)\b",
    re.IGNORECASE,
)
_RISKY_URL = re.compile(
    r"/(cart|checkout|pay|payment|billing|order|purchase|transfer|compose|"
    r"settings/(security|account)|delete)\b",
    re.IGNORECASE,
)
_SECRET_FIELD = re.compile(
    r"(password|passwd|otp|2fa|cvv|cvc|card.?number|security.?code|secret|token)",
    re.IGNORECASE,
)


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


def _confirm_ttl() -> int:
    return _int_env("SHADOW_BROWSER_CONFIRM_TTL_SECONDS", 300)


def _rate_limit_per_min() -> int:
    return _int_env("SHADOW_BROWSER_ACTIONS_PER_MINUTE", 30)


def _action_timeout_ms() -> int:
    return _int_env("SHADOW_BROWSER_ACTION_TIMEOUT_SECONDS", 30) * 1000


def _nav_timeout_ms() -> int:
    return _int_env("SHADOW_BROWSER_NAV_TIMEOUT_SECONDS", 45) * 1000


def _idle_close_seconds() -> int:
    return _int_env("SHADOW_BROWSER_IDLE_CLOSE_SECONDS", 900)


def _max_contexts() -> int:
    return _int_env("SHADOW_BROWSER_MAX_CONTEXTS", 3)


def _headful() -> bool:
    return os.getenv("SHADOW_BROWSER_HEADFUL", "").strip().lower() in {"1", "true", "yes", "on"}


def _allow_private() -> bool:
    return os.getenv("SHADOW_BROWSER_ALLOW_PRIVATE", "").strip().lower() in {"1", "true", "yes", "on"}


def _domain_list(name: str) -> list[str]:
    return [d.strip().lower().lstrip(".") for d in (os.getenv(name, "") or "").split(",") if d.strip()]


def _owner_slug(owner: str) -> str:
    clean = re.sub(r"[^a-z0-9]+", "-", str(owner or "").strip().lower()).strip("-")[:40]
    if not clean:
        raise BrowserError("Browser actions require a real account owner")
    return clean


def _redact_params(action: str, params: dict[str, Any]) -> dict[str, Any]:
    shown = {k: v for k, v in params.items() if k != "value"}
    if "value" in params:
        shown["value"] = "***"
    if action == "eval":
        shown["js"] = str(params.get("js") or "")[:200]
    return shown


def _page_context(url: str) -> tuple[str, str, str]:
    """Scheme + host + path — the parts that define what an approval applied to."""
    try:
        parts = urlsplit(str(url or ""))
        return (parts.scheme, parts.hostname or "", parts.path or "/")
    except ValueError:
        return ("", "", "")


def _safe_url_for_log(url: str) -> str:
    """Drop query/fragment — they routinely carry tokens."""
    try:
        parts = urlsplit(str(url or ""))
        return f"{parts.scheme}://{parts.netloc}{parts.path}"[:200]
    except ValueError:
        return "(unparseable)"


def _domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _is_private_host(host: str) -> bool:
    """True for hostnames that are obviously local/private without DNS."""
    host = (host or "").strip().lower().rstrip(".")
    if host in {"localhost", "ip6-localhost", "metadata.google.internal"} or host.endswith(".local") or host.endswith(".internal"):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


async def _resolves_private(host: str) -> bool:
    """DNS-level guard: refuse names that resolve only to private space."""
    try:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(loop.getaddrinfo(host, 443), timeout=5)
    except Exception:
        return False  # let Playwright surface the real DNS error
    addrs = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return bool(addrs) and all(not a.is_global for a in addrs)


async def check_url_allowed(url: str) -> tuple[bool, str]:
    """Validate scheme, allow/deny lists, and private-network targets."""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme not in {"http", "https"}:
        return False, f"Only http/https URLs are allowed (got {parts.scheme or 'no scheme'})"
    host = (parts.hostname or "").lower()
    if not host:
        return False, "URL has no host"
    for domain in _domain_list("SHADOW_BROWSER_DENY_DOMAINS"):
        if _domain_matches(host, domain):
            return False, f"Domain {host} is on the deny list"
    allow = _domain_list("SHADOW_BROWSER_ALLOW_DOMAINS")
    if allow and not any(_domain_matches(host, domain) for domain in allow):
        return False, f"Domain {host} is not on the allow list"
    if not _allow_private():
        if _is_private_host(host):
            return False, f"{host} is a private/internal address (set SHADOW_BROWSER_ALLOW_PRIVATE=1 to permit)"
        if not allow and await _resolves_private(host):
            return False, f"{host} resolves to a private/internal address"
    return True, ""


def classify_risk(action: str, params: dict[str, Any], page_url: str = "", element_text: str = "") -> tuple[bool, str]:
    """Decide whether an action must wait for human confirmation.

    Pure and unit-testable. Errs toward gating: a wrongly-gated click costs
    one approval tap; a wrongly-allowed purchase is irreversible.
    """
    action = (action or "").strip().lower()
    if action in READ_ACTIONS or action in {"navigate", "back", "forward", "reload", "download", "switch_tab"}:
        return False, ""
    if action == "eval":
        return True, "arbitrary JavaScript execution"
    haystack = " ".join(
        str(params.get(key) or "") for key in ("selector", "text")
    ) + " " + (element_text or "")
    if action == "fill":
        if _SECRET_FIELD.search(str(params.get("selector") or "") + " " + (element_text or "")):
            return True, "typing into a password/secret field"
        return False, ""
    if action in {"click", "press"}:
        if action == "press" and str(params.get("key") or "").lower() not in {"enter", "return"}:
            return False, ""
        match = _RISKY_WORDS.search(haystack)
        if match:
            return True, f"matches irreversible-action keyword '{match.group(0)}'"
        url_match = _RISKY_URL.search(page_url or "")
        if url_match:
            return True, f"page path looks transactional ('{url_match.group(0)}')"
        return False, ""
    return True, f"unknown action '{action}'"


class _OwnerSession:
    """One persistent Chromium context plus per-owner bookkeeping."""

    def __init__(self, owner: str):
        self.owner = owner
        self.slug = _owner_slug(owner)
        self.context = None
        self.pages: list[Any] = []
        self.lock = asyncio.Lock()
        self.last_used = time.time()
        self.history: deque[dict[str, Any]] = deque(maxlen=200)
        self.action_times: deque[float] = deque(maxlen=200)
        self.downloads: list[str] = []

    @property
    def page(self):
        self.pages = [p for p in self.pages if not p.is_closed()]
        if not self.pages:
            return None
        return self.pages[-1]

    def note(self, action: str, detail: str, *, ok: bool = True, gated: bool = False) -> None:
        self.history.append({
            "ts": time.time(),
            "action": action,
            "detail": detail[:300],
            "ok": ok,
            "gated": gated,
        })


class BrowserManager:
    """All live Playwright state. One instance per app process."""

    def __init__(self):
        self._playwright = None
        self._sessions: dict[str, _OwnerSession] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None

    # ── lifecycle ───────────────────────────────────────────────────────

    async def _ensure_playwright(self):
        if self._playwright is not None:
            return self._playwright
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserError(
                "Playwright is not installed. Run `pip install playwright && "
                "python -m playwright install chromium` and restart Shadow."
            ) from exc
        self._playwright = await async_playwright().start()
        return self._playwright

    async def _session(self, owner: str) -> _OwnerSession:
        owner = str(owner or "").strip().lower()
        slug = _owner_slug(owner)
        async with self._lock:
            sess = self._sessions.get(slug)
            if sess is None:
                sess = _OwnerSession(owner)
                self._sessions[slug] = sess
        async with sess.lock:
            if sess.context is None:
                await self._evict_idle(keep_slug=slug)
                sess.context = await self._launch_context(sess)
        sess.last_used = time.time()
        return sess

    async def _launch_context(self, sess: _OwnerSession):
        playwright = await self._ensure_playwright()
        profile_dir = DATA_ROOT / "profiles" / sess.slug
        downloads_dir = DATA_ROOT / "downloads" / sess.slug
        for path in (DATA_ROOT, DATA_ROOT / "profiles", profile_dir, downloads_dir, DATA_ROOT / "shots" / sess.slug):
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, 0o700)

        async def _launch(headless: bool):
            return await playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=headless,
                viewport=VIEWPORT,
                accept_downloads=True,
                args=["--disable-dev-shm-usage"],
            )

        try:
            context = await _launch(headless=not _headful())
        except Exception as exc:
            if _headful():
                logger.warning("Headful Chromium failed (%s); retrying headless", exc)
                context = await _launch(headless=True)
            else:
                raise BrowserError(f"Chromium failed to start: {exc}") from exc

        context.set_default_timeout(_action_timeout_ms())
        context.set_default_navigation_timeout(_nav_timeout_ms())

        async def _route_guard(route):
            host = (urlsplit(route.request.url).hostname or "").lower()
            scheme = urlsplit(route.request.url).scheme
            if scheme in {"http", "https"} and (_allow_private() or not _is_private_host(host)):
                await route.continue_()
            else:
                await route.abort()

        await context.route("**/*", _route_guard)

        def _on_page(page):
            sess.pages.append(page)

        async def _on_download(download):
            try:
                target = downloads_dir / re.sub(r"[^A-Za-z0-9._-]", "_", download.suggested_filename or "download")[:120]
                await download.save_as(str(target))
                sess.downloads.append(str(target))
                sess.note("download", f"saved {target.name}")
            except Exception as exc:  # noqa: BLE001 — a failed download must not kill the page
                sess.note("download", f"failed: {exc}", ok=False)

        context.on("page", _on_page)
        context.on("download", lambda d: asyncio.ensure_future(_on_download(d)))
        sess.pages = list(context.pages)
        if not sess.pages:
            sess.pages = [await context.new_page()]

        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_idle_loop())
        logger.info("Browser context started for owner=%s (headless=%s)", sess.slug, not _headful())
        return context

    async def _evict_idle(self, keep_slug: str) -> None:
        """LRU-close other live contexts when at the cap (caller holds no session locks)."""
        live = [s for s in self._sessions.values() if s.context is not None and s.slug != keep_slug]
        overflow = len(live) + 1 - _max_contexts()
        for sess in sorted(live, key=lambda s: s.last_used)[: max(0, overflow)]:
            await self._close_session(sess)

    async def _close_session(self, sess: _OwnerSession) -> None:
        context, sess.context, sess.pages = sess.context, None, []
        if context is not None:
            try:
                await context.close()
                logger.info("Browser context closed for owner=%s", sess.slug)
            except Exception as exc:  # noqa: BLE001 — already-dead contexts are fine
                logger.debug("Browser context close for %s: %s", sess.slug, exc)

    async def _reap_idle_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            cutoff = time.time() - _idle_close_seconds()
            for sess in list(self._sessions.values()):
                if sess.context is not None and sess.last_used < cutoff:
                    async with sess.lock:
                        if sess.context is not None and sess.last_used < cutoff:
                            await self._close_session(sess)

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
        for sess in list(self._sessions.values()):
            await self._close_session(sess)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Playwright stop: %s", exc)
            self._playwright = None

    # ── pending approvals ───────────────────────────────────────────────

    def _purge_pending(self) -> None:
        now = time.time()
        for key in [k for k, v in self._pending.items() if v["expires_at"] <= now]:
            self._pending.pop(key, None)

    _PENDING_PRIVATE_KEYS = {"params", "page_url"}

    def list_pending(self, owner: str) -> list[dict[str, Any]]:
        self._purge_pending()
        owner = str(owner or "").strip().lower()
        return [
            {k: v for k, v in item.items() if k not in self._PENDING_PRIVATE_KEYS}
            for item in self._pending.values()
            if item["owner"] == owner
        ]

    def _add_pending(self, owner: str, action: str, params: dict[str, Any], reason: str, requested_by: str, page_url: str) -> dict[str, Any]:
        self._purge_pending()
        now = time.time()
        item = {
            "id": secrets.token_urlsafe(12),
            "owner": owner,
            "action": action,
            "params": dict(params),  # full params stay in memory only
            "page_url": str(page_url or ""),  # memory only; used to detect context drift
            "display": _redact_params(action, params),
            "reason": reason,
            "page": _safe_url_for_log(page_url),
            "requested_by": str(requested_by or "unknown")[:100],
            "created_at": now,
            "expires_at": now + _confirm_ttl(),
        }
        self._pending[item["id"]] = item
        return {k: v for k, v in item.items() if k not in self._PENDING_PRIVATE_KEYS}

    async def confirm(self, owner: str, pending_id: str) -> dict[str, Any]:
        self._purge_pending()
        owner = str(owner or "").strip().lower()
        item = self._pending.get(pending_id)
        if not item or item["owner"] != owner:
            raise BrowserError("Pending browser action not found (it may have expired)")
        self._pending.pop(pending_id, None)
        # The user approved the action as seen on a specific page. If the page
        # navigated since, the same selector/click could hit something else
        # entirely — fail closed and make them re-request on the new page.
        sess = self._sessions.get(_owner_slug(owner))
        current_url = sess.page.url if sess and sess.page else ""
        if _page_context(current_url) != _page_context(item.get("page_url") or ""):
            raise BrowserConfirmContextError(
                "The page changed since this approval was requested "
                f"(was {_safe_url_for_log(item.get('page_url') or '')!r}, "
                f"now {_safe_url_for_log(current_url)!r}). Re-run the action and approve again."
            )
        logger.info("Browser gated action approved owner=%s action=%s", _owner_slug(owner), item["action"])
        return await self.run_action(owner, item["action"], item["params"], requested_by=f"approved:{owner}", confirmed=True)

    def cancel(self, owner: str, pending_id: str) -> dict[str, Any]:
        self._purge_pending()
        owner = str(owner or "").strip().lower()
        item = self._pending.get(pending_id)
        if not item or item["owner"] != owner:
            raise BrowserError("Pending browser action not found (it may have expired)")
        self._pending.pop(pending_id, None)
        return {"status": "cancelled", "id": pending_id}

    # ── actions ─────────────────────────────────────────────────────────

    def _check_rate(self, sess: _OwnerSession) -> None:
        now = time.time()
        while sess.action_times and sess.action_times[0] < now - 60:
            sess.action_times.popleft()
        if len(sess.action_times) >= _rate_limit_per_min():
            raise BrowserError(
                f"Browser rate limit reached ({_rate_limit_per_min()} actions/minute). Wait a moment."
            )
        sess.action_times.append(now)

    async def run_action(
        self,
        owner: str,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        requested_by: str = "web",
        confirmed: bool = False,
    ) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        params = dict(params or {})
        if action not in ACTIONS:
            raise BrowserError(f"Unsupported browser action: {action or '(missing)'} (valid: {', '.join(sorted(ACTIONS))})")

        if action == "status":
            return await self.status(owner)

        sess = await self._session(owner)
        async with sess.lock:
            # screenshot/tabs are pure-local captures of the already-rendered
            # page; the command-center poll takes one every 10s and must not
            # starve the interactive/agent action budget.
            if action not in {"screenshot", "tabs"}:
                self._check_rate(sess)
            page = sess.page
            if page is None:
                sess.pages = [await sess.context.new_page()]
                page = sess.page
            page_url = page.url

            if not confirmed:
                element_text = ""
                if action == "click" and params.get("text"):
                    element_text = str(params["text"])
                gated, reason = classify_risk(action, params, page_url=page_url, element_text=element_text)
                if gated:
                    pending = self._add_pending(owner, action, params, reason, requested_by, page_url)
                    sess.note(action, f"gated: {reason}", gated=True)
                    logger.info(
                        "Browser action gated owner=%s action=%s reason=%s",
                        sess.slug, action, reason,
                    )
                    return {"status": "pending_confirmation", "pending": pending}

            try:
                result = await self._execute(sess, page, action, params)
            except BrowserError:
                raise
            except Exception as exc:  # noqa: BLE001 — normalize Playwright errors for callers
                detail = str(exc).split("\n")[0][:300]
                sess.note(action, f"failed: {detail}", ok=False)
                logger.info("Browser action failed owner=%s action=%s err=%s", sess.slug, action, detail)
                raise BrowserError(f"Browser {action} failed: {detail}") from exc
            sess.last_used = time.time()
            sess.note(action, result.get("detail", _safe_url_for_log(page.url)))
            logger.info("Browser action ok owner=%s action=%s page=%s", sess.slug, action, _safe_url_for_log(sess.page.url if sess.page else ""))
            return result

    async def _execute(self, sess: _OwnerSession, page, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "navigate":
            url = str(params.get("url") or "").strip()
            if url and "://" not in url:
                url = "https://" + url
            ok, why = await check_url_allowed(url)
            if not ok:
                raise BrowserError(why)
            response = await page.goto(url, wait_until="domcontentloaded")
            ok_after, why_after = await check_url_allowed(page.url)
            if not ok_after:
                await page.goto("about:blank")
                raise BrowserError(f"Redirect landed on a blocked target: {why_after}")
            status = response.status if response else "?"
            title = await page.title()
            return {"status": "ok", "detail": f"{_safe_url_for_log(page.url)} (HTTP {status})",
                    "url": page.url, "title": title, "http_status": status}

        if action == "read":
            selector = str(params.get("selector") or "").strip()
            max_chars = min(int(params.get("max_chars") or 6000), 20000)
            if selector:
                text = await page.locator(selector).first.inner_text(timeout=_action_timeout_ms())
            else:
                text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
            title = await page.title()
            return {"status": "ok", "detail": f"read {len(text)} chars", "url": page.url,
                    "title": title, "text": text[:max_chars], "truncated": len(text) > max_chars}

        if action == "links":
            links = await page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href]')).slice(0, 80)
                       .map(a => ({text: (a.innerText || '').trim().slice(0, 120), href: a.href}))
                       .filter(l => l.text)"""
            )
            return {"status": "ok", "detail": f"{len(links)} links", "url": page.url, "links": links[:50]}

        if action == "click":
            locator = self._locator(page, params)
            await locator.click()
            await page.wait_for_load_state("domcontentloaded")
            return {"status": "ok", "detail": f"clicked, now at {_safe_url_for_log(page.url)}",
                    "url": page.url, "title": await page.title()}

        if action == "fill":
            locator = self._locator(page, params)
            await locator.fill(str(params.get("value") or ""))
            return {"status": "ok", "detail": f"filled {params.get('selector') or params.get('text')}", "url": page.url}

        if action == "press":
            key = str(params.get("key") or "Enter")
            selector = str(params.get("selector") or "").strip()
            if selector:
                await page.locator(selector).first.press(key)
            else:
                await page.keyboard.press(key)
            await page.wait_for_load_state("domcontentloaded")
            return {"status": "ok", "detail": f"pressed {key}", "url": page.url, "title": await page.title()}

        if action == "eval":
            js = str(params.get("js") or "")
            if not js.strip():
                raise BrowserError("eval requires js")
            value = await page.evaluate(js)
            text = str(value)[:4000]
            return {"status": "ok", "detail": "evaluated JS", "url": page.url, "result": text}

        if action == "screenshot":
            return await self._screenshot(sess, page, full_page=bool(params.get("full_page")))

        if action in {"back", "forward", "reload"}:
            await {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[action]()
            await page.wait_for_load_state("domcontentloaded")
            return {"status": "ok", "detail": f"{action} → {_safe_url_for_log(page.url)}", "url": page.url, "title": await page.title()}

        if action == "wait":
            selector = str(params.get("selector") or "").strip()
            ms = min(int(params.get("ms") or 1000), 10000)
            if selector:
                await page.locator(selector).first.wait_for(timeout=ms)
                return {"status": "ok", "detail": f"{selector} appeared", "url": page.url}
            await asyncio.sleep(ms / 1000)
            return {"status": "ok", "detail": f"waited {ms}ms", "url": page.url}

        if action == "download":
            url = str(params.get("url") or "").strip()
            if not url:
                raise BrowserError("download requires url")
            ok, why = await check_url_allowed(url)
            if not ok:
                raise BrowserError(why)
            response = await sess.context.request.get(url, max_redirects=3)
            body = await response.body()
            if len(body) > 50 * 1024 * 1024:
                raise BrowserError("Download larger than the 50MB cap")
            name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(urlsplit(url).path) or "download")[:120]
            target = DATA_ROOT / "downloads" / sess.slug / f"{int(time.time())}-{name}"
            target.write_bytes(body)
            os.chmod(target, 0o600)
            sess.downloads.append(str(target))
            return {"status": "ok", "detail": f"downloaded {name} ({len(body)} bytes)", "path": str(target), "bytes": len(body)}

        if action == "tabs":
            sess.pages = [p for p in sess.pages if not p.is_closed()]
            tabs = []
            for i, p in enumerate(sess.pages):
                tabs.append({"index": i, "url": _safe_url_for_log(p.url), "current": p is page})
            return {"status": "ok", "detail": f"{len(tabs)} tab(s)", "tabs": tabs}

        if action == "switch_tab":
            sess.pages = [p for p in sess.pages if not p.is_closed()]
            index = int(params.get("index") or 0)
            if not 0 <= index < len(sess.pages):
                raise BrowserError(f"No tab {index} (have {len(sess.pages)})")
            chosen = sess.pages.pop(index)
            sess.pages.append(chosen)  # current page == last
            await chosen.bring_to_front()
            return {"status": "ok", "detail": f"switched to {_safe_url_for_log(chosen.url)}", "url": chosen.url}

        raise BrowserError(f"Unhandled action {action}")

    @staticmethod
    def _locator(page, params: dict[str, Any]):
        selector = str(params.get("selector") or "").strip()
        text = str(params.get("text") or "").strip()
        if selector:
            return page.locator(selector).first
        if text:
            return page.get_by_text(text, exact=False).first
        raise BrowserError("This action needs a selector or text to target")

    async def _screenshot(self, sess: _OwnerSession, page, *, full_page: bool = False) -> dict[str, Any]:
        shots_dir = DATA_ROOT / "shots" / sess.slug
        shots_dir.mkdir(parents=True, exist_ok=True)
        shot_id = secrets.token_urlsafe(8)
        target = shots_dir / f"{shot_id}.jpg"
        await page.screenshot(path=str(target), type="jpeg", quality=70, full_page=full_page)
        os.chmod(target, 0o600)
        # keep the store bounded per owner
        shots = sorted(shots_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        for old in shots[:-40]:
            old.unlink(missing_ok=True)
        return {
            "status": "ok",
            "detail": f"screenshot of {_safe_url_for_log(page.url)}",
            "url": page.url,
            "shot_id": shot_id,
            "shot_url": f"/api/browser/shot/{shot_id}",
            "path": str(target),
        }

    def shot_path(self, owner: str, shot_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,32}", str(shot_id or "")):
            raise BrowserError("Invalid screenshot id")
        path = DATA_ROOT / "shots" / _owner_slug(owner) / f"{shot_id}.jpg"
        if not path.exists():
            raise BrowserError("Screenshot not found")
        return path

    # ── introspection ───────────────────────────────────────────────────

    def available(self) -> bool:
        try:
            import playwright  # noqa: F401
            return True
        except ImportError:
            return False

    async def status(self, owner: str) -> dict[str, Any]:
        owner = str(owner or "").strip().lower()
        slug = _owner_slug(owner)
        sess = self._sessions.get(slug)
        page = sess.page if sess else None
        out: dict[str, Any] = {
            "status": "ok",
            "available": self.available(),
            "headful": _headful(),
            "open": bool(sess and sess.context is not None),
            "pending": self.list_pending(owner),
            "detail": "browser status",
        }
        if page is not None and not page.is_closed():
            out["url"] = page.url
            try:
                out["title"] = await page.title()
            except Exception:  # noqa: BLE001 — page may be navigating
                out["title"] = ""
        return out

    def history(self, owner: str) -> list[dict[str, Any]]:
        sess = self._sessions.get(_owner_slug(owner))
        return list(sess.history) if sess else []

    async def close_owner(self, owner: str) -> dict[str, Any]:
        sess = self._sessions.get(_owner_slug(owner))
        if sess is not None:
            async with sess.lock:
                await self._close_session(sess)
        return {"status": "ok", "detail": "browser closed"}

    def wipe_profile(self, owner: str) -> dict[str, Any]:
        """Delete the on-disk profile (cookies/logins). Context must be closed."""
        sess = self._sessions.get(_owner_slug(owner))
        if sess is not None and sess.context is not None:
            raise BrowserError("Close the browser before wiping its profile")
        profile_dir = DATA_ROOT / "profiles" / _owner_slug(owner)
        if profile_dir.exists():
            shutil.rmtree(profile_dir)
        return {"status": "ok", "detail": "profile wiped"}


MANAGER = BrowserManager()


async def run_browse_task(owner: str, instruction: str) -> dict[str, Any]:
    """Bounded non-LLM browse: open a URL (or search it), read, screenshot.

    Used by the Telegram bridge and as MAGI's shared evidence source. Returns
    {text, url, title, shot_path}. Gated actions never arise here — it only
    navigates and reads.
    """
    instruction = str(instruction or "").strip()
    if not instruction:
        raise BrowserError("Empty browse instruction")
    target = instruction
    if "://" not in target:
        if "." in target and " " not in target:
            target = "https://" + target
        else:
            from urllib.parse import quote_plus

            target = "https://html.duckduckgo.com/html/?q=" + quote_plus(instruction)
    nav = await MANAGER.run_action(owner, "navigate", {"url": target}, requested_by="browse-task")
    read = await MANAGER.run_action(owner, "read", {"max_chars": 3500}, requested_by="browse-task")
    shot = await MANAGER.run_action(owner, "screenshot", {}, requested_by="browse-task")
    return {
        "url": nav.get("url"),
        "title": read.get("title") or nav.get("title") or "",
        "text": read.get("text") or "",
        "shot_path": shot.get("path"),
        "shot_id": shot.get("shot_id"),
    }
