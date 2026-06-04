"""Music player routes — free YouTube-backed search + audio streaming.

Search and audio resolution both go through ``yt-dlp`` (no API key, no login).
Resolved audio URLs are IP-locked + short-lived, so the ``/stream`` endpoint
proxies the bytes through the server and forwards Range requests so the
``<audio>`` element can seek.

Library state (liked songs, playlists, recently played) is persisted per user
as JSON under ``data/music/<user>.json`` — same lightweight pattern as the
other per-user feature stores.

NB: yt-dlp tracks YouTube's frequent changes. If streaming ever stops
resolving, `pip install -U yt-dlp` and rebuild.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.auth_helpers import get_current_user

# --- module state ------------------------------------------------------------

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_DATA_DIR = os.path.join("data", "music")

# videoId -> {"url": str, "headers": dict, "expires": epoch}
_URL_CACHE: Dict[str, Dict[str, Any]] = {}
_URL_CACHE_LOCK = threading.Lock()
_URL_TTL = 5 * 3600  # resolved googlevideo URLs last ~6h; refresh before then

_FILE_LOCK = threading.Lock()

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# bgutil PO-token provider (docker-compose service). The bgutil yt-dlp plugin
# reads the provider URL from this extractor-arg and uses it to get past
# YouTube's datacenter-IP bot check. Override with POT_BASE_URL if needed.
_POT_BASE = os.environ.get("POT_BASE_URL", "http://bgutil-pot:4416")


# --- yt-dlp helpers (blocking; call via asyncio.to_thread) --------------------

def _ydl(opts: Dict[str, Any]):
    import yt_dlp

    base = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        # Cache the solved player JS / nsig functions so only the first track
        # pays the full Deno-solve cost; later tracks resolve much faster.
        "cachedir": os.path.join(_DATA_DIR, ".ytdlp-cache"),
        "extractor_args": {"youtubepot-bgutilhttp": {"base_url": [_POT_BASE]}},
    }
    # Authenticated cookies let requests through even when YouTube has flagged
    # the (datacenter) IP, which PO tokens alone don't fix. Drop a Netscape
    # cookies.txt here (see MUSIC_COOKIES_FILE) to enable playback.
    cookies = os.environ.get("MUSIC_COOKIES_FILE", os.path.join(_DATA_DIR, "cookies.txt"))
    if os.path.exists(cookies):
        base["cookiefile"] = cookies
    # Merge per-call extractor_args on top of the always-on bgutil arg.
    extra_ea = opts.pop("extractor_args", None)
    base.update(opts)
    if extra_ea:
        merged = dict(base["extractor_args"])
        merged.update(extra_ea)
        base["extractor_args"] = merged
    return yt_dlp.YoutubeDL(base)


def _thumb(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def _search_blocking(query: str, limit: int) -> List[Dict[str, Any]]:
    with _ydl({"extract_flat": True, "default_search": "ytsearch"}) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    out: List[Dict[str, Any]] = []
    for e in (info.get("entries") or []):
        vid = e.get("id")
        if not vid or not _VIDEO_ID_RE.match(vid):
            continue
        title = e.get("title") or "Unknown"
        out.append(
            {
                "id": vid,
                "title": title,
                "artist": e.get("uploader") or e.get("channel") or "",
                "duration": int(e.get("duration") or 0),
                "thumb": _thumb(vid),
            }
        )
    return out


def _resolve_blocking(video_id: str) -> Dict[str, Any]:
    """Resolve a direct audio URL (+ any headers it needs) for a video."""
    with _ydl(
        {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "http_headers": {"User-Agent": _UA},
            # Default client (with cookies + bgutil PO + Deno n-sig solving)
            # returns the audio-only DASH formats (140 m4a / 251 opus).
            # Forcing the web client only yields the 360p progressive (18).
        }
    ) as ydl:
        d = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    url = d.get("url")
    if not url:
        # format-selection or single-format edge case: dig into formats
        fmts = [f for f in (d.get("formats") or []) if f.get("acodec") not in (None, "none") and f.get("url")]
        fmts.sort(key=lambda f: (f.get("vcodec") not in (None, "none"), -(f.get("abr") or 0)))
        if fmts:
            url = fmts[0]["url"]
            d = fmts[0]
    if not url:
        raise RuntimeError("no audio stream found")
    headers = dict(d.get("http_headers") or {})
    headers.setdefault("User-Agent", _UA)
    return {"url": url, "headers": headers}


async def _get_audio(video_id: str) -> Dict[str, Any]:
    now = time.time()
    with _URL_CACHE_LOCK:
        hit = _URL_CACHE.get(video_id)
        if hit and hit["expires"] > now:
            return hit
    resolved = await asyncio.to_thread(_resolve_blocking, video_id)
    entry = {"url": resolved["url"], "headers": resolved["headers"], "expires": now + _URL_TTL}
    with _URL_CACHE_LOCK:
        _URL_CACHE[video_id] = entry
    return entry


# --- per-user library store --------------------------------------------------

def _slug(user: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", (user or "anon").lower()) or "anon"


def _lib_path(user: Optional[str]) -> str:
    os.makedirs(_DATA_DIR, exist_ok=True)
    return os.path.join(_DATA_DIR, f"{_slug(user)}.json")


def _load_lib(user: Optional[str]) -> Dict[str, Any]:
    path = _lib_path(user)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("liked", [])
    data.setdefault("playlists", [])
    data.setdefault("recent", [])
    return data


def _save_lib(user: Optional[str], data: Dict[str, Any]) -> None:
    path = _lib_path(user)
    with _FILE_LOCK:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def _clean_track(t: Dict[str, Any]) -> Dict[str, Any]:
    vid = str(t.get("id") or "")
    if not _VIDEO_ID_RE.match(vid):
        raise HTTPException(status_code=400, detail="bad track id")
    return {
        "id": vid,
        "title": str(t.get("title") or "Unknown")[:300],
        "artist": str(t.get("artist") or "")[:200],
        "duration": int(t.get("duration") or 0),
        "thumb": str(t.get("thumb") or _thumb(vid))[:500],
    }


# --- request models ----------------------------------------------------------

class Track(BaseModel):
    id: str
    title: Optional[str] = None
    artist: Optional[str] = None
    duration: Optional[int] = 0
    thumb: Optional[str] = None


class PlaylistCreate(BaseModel):
    name: str


# --- router ------------------------------------------------------------------

def setup_music_routes() -> APIRouter:
    router = APIRouter(prefix="/api/music", tags=["music"])

    @router.get("/search")
    async def search(q: str, limit: int = 25):
        q = (q or "").strip()
        if not q:
            return {"results": []}
        limit = max(1, min(int(limit or 25), 40))
        try:
            results = await asyncio.to_thread(_search_blocking, q, limit)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"search failed: {exc}")
        return {"results": results}

    @router.get("/stream/{video_id}")
    async def stream(video_id: str, request: Request):
        if not _VIDEO_ID_RE.match(video_id):
            raise HTTPException(status_code=400, detail="bad video id")
        try:
            audio = await _get_audio(video_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"resolve failed: {exc}")

        up_headers = dict(audio["headers"])
        rng = request.headers.get("range")
        if rng:
            up_headers["Range"] = rng

        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None), follow_redirects=True)
        try:
            req = client.build_request("GET", audio["url"], headers=up_headers)
            resp = await client.send(req, stream=True)
        except Exception as exc:  # noqa: BLE001
            await client.aclose()
            # URL may have expired mid-flight — drop cache so next try re-resolves
            with _URL_CACHE_LOCK:
                _URL_CACHE.pop(video_id, None)
            raise HTTPException(status_code=502, detail=f"stream failed: {exc}")

        if resp.status_code >= 400:
            await resp.aclose()
            await client.aclose()
            with _URL_CACHE_LOCK:
                _URL_CACHE.pop(video_id, None)
            raise HTTPException(status_code=502, detail=f"upstream {resp.status_code}")

        passthrough = {}
        for h in ("content-type", "content-length", "content-range", "accept-ranges"):
            if h in resp.headers:
                passthrough[h] = resp.headers[h]
        passthrough.setdefault("accept-ranges", "bytes")
        passthrough["cache-control"] = "no-store"

        async def body():
            try:
                async for chunk in resp.aiter_bytes(65536):
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            body(),
            status_code=resp.status_code,
            headers=passthrough,
            media_type=passthrough.get("content-type", "audio/mp4"),
        )

    # --- library -------------------------------------------------------------

    @router.get("/library")
    def get_library(request: Request):
        return _load_lib(get_current_user(request))

    @router.post("/like")
    def add_like(request: Request, track: Track):
        user = get_current_user(request)
        lib = _load_lib(user)
        t = _clean_track(track.model_dump())
        lib["liked"] = [x for x in lib["liked"] if x.get("id") != t["id"]]
        lib["liked"].insert(0, t)
        _save_lib(user, lib)
        return {"ok": True, "liked": lib["liked"]}

    @router.delete("/like/{video_id}")
    def remove_like(request: Request, video_id: str):
        user = get_current_user(request)
        lib = _load_lib(user)
        lib["liked"] = [x for x in lib["liked"] if x.get("id") != video_id]
        _save_lib(user, lib)
        return {"ok": True, "liked": lib["liked"]}

    @router.post("/recent")
    def add_recent(request: Request, track: Track):
        user = get_current_user(request)
        lib = _load_lib(user)
        t = _clean_track(track.model_dump())
        lib["recent"] = [x for x in lib["recent"] if x.get("id") != t["id"]]
        lib["recent"].insert(0, t)
        lib["recent"] = lib["recent"][:50]
        _save_lib(user, lib)
        return {"ok": True}

    @router.post("/playlist")
    def create_playlist(request: Request, body: PlaylistCreate):
        user = get_current_user(request)
        name = (body.name or "").strip()[:120]
        if not name:
            raise HTTPException(status_code=400, detail="name required")
        lib = _load_lib(user)
        pl = {"id": f"pl_{int(time.time()*1000)}", "name": name, "tracks": []}
        lib["playlists"].insert(0, pl)
        _save_lib(user, lib)
        return {"ok": True, "playlist": pl}

    @router.delete("/playlist/{playlist_id}")
    def delete_playlist(request: Request, playlist_id: str):
        user = get_current_user(request)
        lib = _load_lib(user)
        lib["playlists"] = [p for p in lib["playlists"] if p.get("id") != playlist_id]
        _save_lib(user, lib)
        return {"ok": True}

    @router.post("/playlist/{playlist_id}/tracks")
    def add_to_playlist(request: Request, playlist_id: str, track: Track):
        user = get_current_user(request)
        lib = _load_lib(user)
        t = _clean_track(track.model_dump())
        for p in lib["playlists"]:
            if p.get("id") == playlist_id:
                p.setdefault("tracks", [])
                p["tracks"] = [x for x in p["tracks"] if x.get("id") != t["id"]]
                p["tracks"].append(t)
                _save_lib(user, lib)
                return {"ok": True, "playlist": p}
        raise HTTPException(status_code=404, detail="playlist not found")

    @router.delete("/playlist/{playlist_id}/tracks/{video_id}")
    def remove_from_playlist(request: Request, playlist_id: str, video_id: str):
        user = get_current_user(request)
        lib = _load_lib(user)
        for p in lib["playlists"]:
            if p.get("id") == playlist_id:
                p["tracks"] = [x for x in p.get("tracks", []) if x.get("id") != video_id]
                _save_lib(user, lib)
                return {"ok": True, "playlist": p}
        raise HTTPException(status_code=404, detail="playlist not found")

    return router
