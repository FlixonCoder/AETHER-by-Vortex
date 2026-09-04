"""
External space-data proxies: CelesTrak TLEs and NASA imagery.

The browser cannot call these directly — CelesTrak sends no CORS header, and
proxying also lets us cache aggressively so a room full of demo laptops does
not hammer the upstream services. Every response is served from a memory cache
with a long TTL; TLE sets only change a few times per day.
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

# CelesTrak returns 403 to clients that re-fetch too often or send no identifying
# agent. Cache to disk so restarts (and a demo run) never re-hit the upstream.
CACHE_DIR = Path(__file__).parent.parent / ".cache" / "space"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
USER_AGENT = "SatOps-AI-Dashboard/1.0 (student mission-ops demo; contact via localhost)"

CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php"
CELESTRAK_SUPPLEMENTAL = "https://celestrak.org/NORAD/elements/supplemental/sup-gp.php"
# CelesTrak throttles the big constellation groups on gp.php harder than the
# supplemental feed, so these fall back to the supplemental file on 403.
SUPPLEMENTAL_FALLBACK = {"starlink": "starlink", "gps-ops": "gps", "galileo": "galileo"}
NASA_EPIC = "https://api.nasa.gov/EPIC/api/natural"
NASA_API_KEY = "DEMO_KEY"

# CelesTrak asks that clients not re-fetch a group more than a few times a day.
TLE_TTL_S = 3 * 3600
EPIC_TTL_S = 30 * 60

# Groups the UI is allowed to request, mapped to a sane cap on how many
# satellites we return. Starlink alone is ~10.7k objects; propagating all of
# them in the browser at 60fps is not realistic, so we trim server-side.
ALLOWED_GROUPS: Dict[str, int] = {
    "stations": 100,
    "starlink": 1200,
    "gps-ops": 40,
    "galileo": 40,
    "weather": 80,
    "science": 120,
    "geo": 200,
    "active": 1500,
}

_cache: Dict[str, Tuple[float, Any]] = {}


def _disk_path(key: str) -> Path:
    return CACHE_DIR / (key.replace(":", "_") + ".json")


def _cached(key: str, ttl: int) -> Optional[Any]:
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < ttl:
        return hit[1]

    # Fall back to the on-disk copy so a restart doesn't re-hit a rate-limited API.
    p = _disk_path(key)
    if p.exists():
        try:
            blob = json.loads(p.read_text(encoding="utf-8"))
            if (time.time() - blob["fetched_at"]) < ttl:
                _cache[key] = (blob["fetched_at"], blob["value"])
                return blob["value"]
        except Exception:
            pass
    return None


def _stale(key: str) -> Optional[Any]:
    """Any cached copy regardless of age — better than an empty globe."""
    hit = _cache.get(key)
    if hit:
        return hit[1]
    p = _disk_path(key)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))["value"]
        except Exception:
            return None
    return None


def _store(key: str, value: Any) -> Any:
    now = time.time()
    _cache[key] = (now, value)
    try:
        _disk_path(key).write_text(json.dumps({"fetched_at": now, "value": value}), encoding="utf-8")
    except Exception:
        pass
    return value


def _parse_tle(text: str, limit: int) -> list:
    """Three-line TLE text -> [{name, l1, l2}]."""
    lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
    out = []
    for i in range(0, len(lines) - 2, 3):
        name, l1, l2 = lines[i].strip(), lines[i + 1], lines[i + 2]
        if not (l1.startswith("1 ") and l2.startswith("2 ")):
            continue
        out.append({"name": name, "l1": l1, "l2": l2})
        if len(out) >= limit:
            break
    return out


router = APIRouter(prefix="/api/space", tags=["space-data"])


@router.get("/tle/{group}")
async def get_tle(group: str):
    """Live orbital elements for a CelesTrak group."""
    group = group.lower().strip()
    if group not in ALLOWED_GROUPS:
        raise HTTPException(400, f"Unknown group '{group}'. Allowed: {sorted(ALLOWED_GROUPS)}")

    key = f"tle:{group}"
    cached = _cached(key, TLE_TTL_S)
    if cached is not None:
        return {**cached, "cached": True}

    try:
        sats = []
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True,
                                     headers={"User-Agent": USER_AGENT}) as c:
            try:
                r = await c.get(CELESTRAK, params={"GROUP": group, "FORMAT": "tle"})
                r.raise_for_status()
                sats = _parse_tle(r.text, ALLOWED_GROUPS[group])
            except Exception:
                # Primary feed throttled — try the supplemental file for the
                # large constellations, which is rate-limited far less.
                if group not in SUPPLEMENTAL_FALLBACK:
                    raise
                r = await c.get(CELESTRAK_SUPPLEMENTAL,
                                params={"FILE": SUPPLEMENTAL_FALLBACK[group], "FORMAT": "tle"})
                r.raise_for_status()
                sats = _parse_tle(r.text, ALLOWED_GROUPS[group])
        if not sats:
            raise ValueError("empty TLE set")
    except Exception as e:
        # Serve a stale copy rather than blanking the globe mid-demo.
        stale = _stale(key)
        if stale:
            return {**stale, "cached": True, "stale": True}
        raise HTTPException(502, f"CelesTrak unavailable: {e}")

    return _store(key, {"group": group, "count": len(sats), "satellites": sats, "cached": False})


@router.get("/earth-texture")
async def earth_texture(width: int = 2048):
    """Equirectangular Earth texture built from NASA GIBS VIIRS true-colour imagery.

    Served from disk so WebGL gets it same-origin and instantly on later loads.
    VIIRS is a swath sensor, so the most recent day is only partially imaged —
    we step back until we find a date with full global coverage.
    """
    import datetime

    width = 1024 if width < 1536 else 2048
    for back in (2, 3, 4):
        day = (datetime.date.today() - datetime.timedelta(days=back)).isoformat()
        cache_file = CACHE_DIR / f"earth_{day}_{width}.jpg"
        if cache_file.exists() and cache_file.stat().st_size > 20_000:
            return FileResponse(cache_file, media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=86400",
                                         "X-Imagery-Date": day})
        url = (
            "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"
            "?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
            "&LAYERS=VIIRS_SNPP_CorrectedReflectance_TrueColor"
            "&CRS=EPSG:4326&BBOX=-90,-180,90,180"
            f"&WIDTH={width}&HEIGHT={width // 2}&FORMAT=image/jpeg&TIME={day}"
        )
        try:
            async with httpx.AsyncClient(timeout=90.0, follow_redirects=True,
                                         headers={"User-Agent": USER_AGENT}) as c:
                r = await c.get(url)
                r.raise_for_status()
            if len(r.content) > 20_000:
                cache_file.write_bytes(r.content)
                return FileResponse(cache_file, media_type="image/jpeg",
                                    headers={"Cache-Control": "public, max-age=86400",
                                             "X-Imagery-Date": day})
        except Exception:
            continue

    # Fall back to the bundled static texture so the globe is never blank.
    fallback = Path(__file__).parent.parent / "static" / "assets" / "earth.jpg"
    if fallback.exists():
        return FileResponse(fallback, media_type="image/jpeg",
                            headers={"X-Imagery-Date": "static-fallback"})
    raise HTTPException(502, "No Earth imagery available")


@router.get("/night-texture")
async def night_texture(width: int = 2048):
    """NASA Black Marble city-lights texture for the night side of the globe.

    Black Marble is a fixed-epoch composite rather than a daily product, so it
    is requested at its published date and cached indefinitely.
    """
    width = 1024 if width < 1536 else 2048
    cache_file = CACHE_DIR / f"night_{width}.jpg"
    if cache_file.exists() and cache_file.stat().st_size > 20_000:
        return FileResponse(cache_file, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=604800"})

    url = (
        "https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi"
        "?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=VIIRS_Black_Marble"
        "&CRS=EPSG:4326&BBOX=-90,-180,90,180"
        f"&WIDTH={width}&HEIGHT={width // 2}&FORMAT=image/jpeg&TIME=2016-01-01"
    )
    try:
        async with httpx.AsyncClient(timeout=90.0, follow_redirects=True,
                                     headers={"User-Agent": USER_AGENT}) as c:
            r = await c.get(url)
            r.raise_for_status()
        if len(r.content) > 20_000:
            cache_file.write_bytes(r.content)
            return FileResponse(cache_file, media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=604800"})
    except Exception as e:
        raise HTTPException(502, f"NASA Black Marble unavailable: {e}")
    raise HTTPException(502, "NASA Black Marble returned an empty image")


@router.get("/epic")
async def get_epic():
    """Latest real full-disk Earth photographs from NASA's DSCOVR/EPIC camera."""
    cached = _cached("epic", EPIC_TTL_S)
    if cached is not None:
        return {**cached, "cached": True}

    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True,
                                     headers={"User-Agent": USER_AGENT}) as c:
            r = await c.get(NASA_EPIC, params={"api_key": NASA_API_KEY})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        stale = _stale("epic")
        if stale:
            return {**stale, "cached": True, "stale": True}
        raise HTTPException(502, f"NASA EPIC unavailable: {e}")

    frames = []
    for item in data[:12]:
        # date is "YYYY-MM-DD HH:MM:SS"; the archive path wants the date part split.
        d = item.get("date", "").split(" ")[0].replace("-", "/")
        frames.append({
            "caption": item.get("caption", ""),
            "date": item.get("date", ""),
            "image": f"https://api.nasa.gov/EPIC/archive/natural/{d}/png/{item['image']}.png?api_key={NASA_API_KEY}",
            "thumb": f"https://api.nasa.gov/EPIC/archive/natural/{d}/thumbs/{item['image']}.jpg?api_key={NASA_API_KEY}",
            "centroid": item.get("centroid_coordinates", {}),
        })

    return _store("epic", {"count": len(frames), "frames": frames, "cached": False})
