#!/usr/bin/env python3
"""carthing-weatherstation API.

Serves a weather + clock + Spotify dashboard for a Spotify Car Thing (800x480)
running the Nocturne Bluetooth firmware as a kiosk. Weather/forecast via the free
Open-Meteo API (no key); Spotify now-playing + transport via the Spotify Web API
(Authorization Code + PKCE; audio plays on the user's own Connect device).

No database, no birds — this is the standalone weather-station sibling of BirdThing.
"""
import os, json, time, threading, urllib.request, urllib.parse, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(BASE, "weatherstation.html")
ASSETS = os.path.join(BASE, "assets")
WCONF = os.path.join(BASE, "weather.json")
SP_CONF = os.path.join(BASE, "spotify.json")
PORT = int(os.environ.get("WS_PORT", "8095"))

# WMO weather code -> (icon key for the UI's line-SVG set, short description)
WMO = {0:("clear","Clear"),1:("mclear","Mainly clear"),2:("partly","Partly cloudy"),
 3:("cloud","Overcast"),45:("fog","Fog"),48:("fog","Rime fog"),
 51:("drizzle","Light drizzle"),53:("drizzle","Drizzle"),55:("drizzle","Heavy drizzle"),
 56:("drizzle","Freezing drizzle"),57:("drizzle","Freezing drizzle"),
 61:("rain","Light rain"),63:("rain","Rain"),65:("rain","Heavy rain"),
 66:("rain","Freezing rain"),67:("rain","Freezing rain"),
 71:("snow","Light snow"),73:("snow","Snow"),75:("snow","Heavy snow"),77:("snow","Snow grains"),
 80:("rain","Showers"),81:("rain","Showers"),82:("rain","Heavy showers"),
 85:("snow","Snow showers"),86:("snow","Snow showers"),
 95:("storm","Thunderstorm"),96:("storm","Thunderstorm"),99:("storm","Thunderstorm")}
DOW = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]


def load_wconf():
    c = {"lat": 44.6701, "lon": -74.9774, "unit": "C", "place": "Potsdam, NY"}
    try:
        c.update(json.load(open(WCONF)))
    except Exception:
        pass
    return c

def save_wconf(c):
    try:
        json.dump(c, open(WCONF, "w"))
    except Exception:
        pass

def tz_off_min():
    # Local UTC offset in minutes east of UTC (e.g. EDT = -240). The Car Thing has
    # no RTC/NTP and a wrong clock+TZ, so the dashboard renders time from this.
    is_dst = time.localtime().tm_isdst > 0
    secs_west = time.altzone if is_dst else time.timezone
    return -secs_west // 60

def _wmo(code):
    return WMO.get(int(code), ("cloud", "—"))

# Indoor temperature from a BMP280 on the Pi's I2C bus 1 @ 0x77.
_bmp = {"bus": None, "cal": None}
def _read_indoor_c():
    try:
        import smbus, struct
        a = 0x77
        if _bmp["bus"] is None:
            b = smbus.SMBus(1)
            cal = b.read_i2c_block_data(a, 0x88, 6)
            T1 = cal[0] | (cal[1] << 8)
            T2 = struct.unpack("<h", bytes(cal[2:4]))[0]
            T3 = struct.unpack("<h", bytes(cal[4:6]))[0]
            b.write_byte_data(a, 0xF4, 0x27)   # temp+press oversample x1, normal mode
            _bmp["bus"], _bmp["cal"] = b, (T1, T2, T3)
        b = _bmp["bus"]; T1, T2, T3 = _bmp["cal"]
        d = b.read_i2c_block_data(a, 0xFA, 3)
        adc = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        v1 = (adc / 16384.0 - T1 / 1024.0) * T2
        v2 = ((adc / 131072.0 - T1 / 8192.0) ** 2) * T3
        return (v1 + v2) / 5120.0
    except Exception:
        _bmp["bus"] = None
        return None

def _get_json(url, timeout=8):
    return json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "carthing-weatherstation/1.0 (weatherthing)"}),
        timeout=timeout))

def _icon_from_text(s):
    # Map a National Weather Service shortForecast string to our UI icon key.
    t = (s or "").lower()
    if "thunder" in t: return "storm"
    if any(w in t for w in ("snow", "flurr", "sleet", "ice", "wintry", "blizzard")): return "snow"
    if "freezing" in t or "drizzle" in t: return "drizzle"
    if any(w in t for w in ("rain", "shower")): return "rain"
    if any(w in t for w in ("fog", "haze", "smoke", "mist")): return "fog"
    if "partly" in t or "few clouds" in t: return "partly"
    if any(w in t for w in ("mostly cloudy", "overcast", "broken clouds", "cloudy")): return "cloud"
    if any(w in t for w in ("sunny", "clear", "fair")): return "clear"
    return "cloud"

def _weather_openmeteo(c, base):
    imperial = c["unit"] == "F"
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
           "weather_code,wind_speed_10m"
           "&hourly=temperature_2m,weather_code"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset"
           "&forecast_days=7&timezone=auto"
           "&temperature_unit=%s&wind_speed_unit=%s"
           % (c["lat"], c["lon"], "fahrenheit" if imperial else "celsius",
              "mph" if imperial else "kmh"))
    d = _get_json(url, timeout=6)
    cur = d["current"]
    icon, desc = _wmo(cur["weather_code"])
    # hourly: the next 8 hours from "now"
    H = d["hourly"]; times = H["time"]
    nowiso = cur["time"][:13]
    try: start = next(i for i, t in enumerate(times) if t[:13] >= nowiso)
    except StopIteration: start = 0
    hourly = [{"t": times[i][11:16], "temp": round(H["temperature_2m"][i]),
               "icon": _wmo(H["weather_code"][i])[0]}
              for i in range(start, min(start + 8, len(times)))]
    DD = d["daily"]
    daily = [{"date": DD["time"][i],
              "dow": DOW[time.strptime(DD["time"][i], "%Y-%m-%d").tm_wday],
              "icon": _wmo(DD["weather_code"][i])[0],
              "hi": round(DD["temperature_2m_max"][i]),
              "lo": round(DD["temperature_2m_min"][i])} for i in range(len(DD["time"]))]
    base.update({"temp": round(cur["temperature_2m"]), "icon": icon, "desc": desc,
                 "feels": round(cur["apparent_temperature"]),
                 "humidity": round(cur["relative_humidity_2m"]),
                 "wind": round(cur["wind_speed_10m"]),
                 "wind_unit": "mph" if imperial else "km/h",
                 "hi": daily[0]["hi"] if daily else None,
                 "lo": daily[0]["lo"] if daily else None,
                 "hourly": hourly, "daily": daily, "source": "open-meteo",
                 "sunrise": DD["sunrise"][0][11:16], "sunset": DD["sunset"][0][11:16]})
    return base

def _weather_nws(c, base):
    # US National Weather Service fallback (no key, US-only). Used when Open-Meteo is
    # unreachable. Same response shape as the Open-Meteo path.
    units = "us" if c["unit"] == "F" else "si"
    pts = _get_json("https://api.weather.gov/points/%s,%s" % (c["lat"], c["lon"]))
    props = pts["properties"]
    HH = _get_json(props["forecastHourly"] + "?units=" + units)["properties"]["periods"]
    cur = HH[0]
    hourly = [{"t": p["startTime"][11:16], "temp": round(p["temperature"]),
               "icon": _icon_from_text(p.get("shortForecast"))} for p in HH[:8]]
    # sunrise / sunset from isDaytime transitions in the next ~36h (for the auto theme)
    rise = setpt = None
    for i in range(1, min(len(HH), 36)):
        if not HH[i - 1]["isDaytime"] and HH[i]["isDaytime"] and rise is None:
            rise = HH[i]["startTime"][11:16]
        if HH[i - 1]["isDaytime"] and not HH[i]["isDaytime"] and setpt is None:
            setpt = HH[i]["startTime"][11:16]
    # daily: NWS gives day/night periods; fold them into hi/lo per date
    DD = _get_json(props["forecast"] + "?units=" + units)["properties"]["periods"]
    days, order = {}, []
    for p in DD:
        dt = p["startTime"][:10]
        if dt not in days:
            days[dt] = {"hi": None, "lo": None, "icon": None}; order.append(dt)
        rec = days[dt]
        if p["isDaytime"]:
            rec["hi"] = round(p["temperature"]); rec["icon"] = _icon_from_text(p.get("shortForecast"))
        else:
            rec["lo"] = round(p["temperature"])
            if rec["icon"] is None: rec["icon"] = _icon_from_text(p.get("shortForecast"))
    daily = []
    for dt in order[:7]:
        rec = days[dt]
        hi = rec["hi"] if rec["hi"] is not None else rec["lo"]
        lo = rec["lo"] if rec["lo"] is not None else rec["hi"]
        daily.append({"date": dt, "dow": DOW[time.strptime(dt, "%Y-%m-%d").tm_wday],
                      "icon": rec["icon"] or "cloud", "hi": hi, "lo": lo})
    base.update({"temp": round(cur["temperature"]),
                 "icon": _icon_from_text(cur.get("shortForecast")),
                 "desc": cur.get("shortForecast") or "—",
                 "hi": daily[0]["hi"] if daily else None,
                 "lo": daily[0]["lo"] if daily else None,
                 "hourly": hourly, "daily": daily, "source": "nws"})
    if rise: base["sunrise"] = rise
    if setpt: base["sunset"] = setpt
    return base

# Cache the last good forecast (keyed on unit+location) so the Car Thing's frequent
# polls return instantly, and a circuit-breaker so a blocked Open-Meteo isn't retried
# on every request (it just wastes seconds until it times out).
_wx = {"data": None, "ts": 0, "key": None}
_om_fail_until = 0
WX_TTL = 150          # serve cached forecast for this many seconds
OM_COOLDOWN = 600     # after Open-Meteo fails, skip it for this long (still retry NWS)

def _compute_weather(c, base):
    global _om_fail_until
    providers = []
    if time.time() >= _om_fail_until:
        providers.append(_weather_openmeteo)     # primary, unless it's in cooldown
    providers.append(_weather_nws)               # US fallback (reachable when OM is blocked)
    errs = []
    for fn in providers:
        try:
            return fn(c, dict(base))
        except Exception as e:
            if fn is _weather_openmeteo:
                _om_fail_until = time.time() + OM_COOLDOWN
            errs.append("%s: %s" % (fn.__name__.replace("_weather_", ""), e))
    base.update({"temp": None, "icon": "cloud", "desc": "—",
                 "hourly": [], "daily": [], "err": " | ".join(errs)})
    return base

def weather(force=False):
    c = load_wconf()
    base = {"unit": c["unit"], "place": c["place"],
            "now": int(time.time() * 1000), "tzoff": tz_off_min()}
    ind = _read_indoor_c()
    if ind is not None:
        base["indoor"] = round(ind * 9 / 5 + 32) if c["unit"] == "F" else round(ind)
    key = (c["unit"], c["lat"], c["lon"])
    if (not force and _wx["data"] and _wx["key"] == key
            and time.time() - _wx["ts"] < WX_TTL and _wx["data"].get("temp") is not None):
        data = dict(_wx["data"]); data.update(base); return data   # cached forecast, fresh clock/indoor
    data = _compute_weather(c, base)
    if data.get("temp") is not None:
        _wx.update({"data": data, "ts": time.time(), "key": key})
    return data

def geocode(q):
    try:
        url = ("https://geocoding-api.open-meteo.com/v1/search?name=%s&count=5"
               % urllib.parse.quote(q))
        res = json.load(urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "carthing-weatherstation/1.0"}),
            timeout=8)).get("results", [])
        out = []
        for r in res:
            place = r["name"]
            if r.get("admin1"): place += ", " + r["admin1"]
            if r.get("country_code"): place += ", " + r["country_code"]
            out.append({"place": place, "lat": r["latitude"], "lon": r["longitude"]})
        return out
    except Exception:
        return []


# ---- Spotify (Web API remote: shows what's playing on the user's phone/Alexa + controls it) ----
# Audio plays on the user's own Spotify Connect device; the display is a remote only.
# Creds in spotify.json next to this file: {"client_id": "...", "refresh_token": "..."}
# (Authorization Code + PKCE — no client secret). Generate with ../spotify_auth.py.
_sp = {"access": None, "exp": 0, "playing": False}

def _sp_conf():
    try:
        return json.load(open(SP_CONF))
    except Exception:
        return None

def _sp_token():
    if _sp["access"] and time.time() < _sp["exp"] - 60:
        return _sp["access"]
    c = _sp_conf()
    if not c or not c.get("refresh_token") or not c.get("client_id"):
        return None
    data = urllib.parse.urlencode({"grant_type": "refresh_token",
        "refresh_token": c["refresh_token"], "client_id": c["client_id"]}).encode()
    try:
        tok = json.load(urllib.request.urlopen(urllib.request.Request(
            "https://accounts.spotify.com/api/token", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=8))
    except Exception:
        return None
    _sp["access"] = tok.get("access_token")
    _sp["exp"] = time.time() + tok.get("expires_in", 3600)
    if tok.get("refresh_token") and tok["refresh_token"] != c["refresh_token"]:
        c["refresh_token"] = tok["refresh_token"]      # Spotify rotates refresh tokens under PKCE
        try: json.dump(c, open(SP_CONF, "w"))
        except Exception: pass
    return _sp["access"]

def _sp_api(method, path, timeout=8):
    t = _sp_token()
    if not t:
        return None, 401
    req = urllib.request.Request("https://api.spotify.com/v1" + path, method=method,
        headers={"Authorization": "Bearer " + t})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        body = r.read()
        return (json.loads(body) if body else None), r.status
    except urllib.error.HTTPError as e:
        return None, e.code
    except Exception:
        return None, 0

_ctx_cache = {}
def _context_name(ctx):
    if not ctx:
        return None
    uri = ctx.get("uri") or ""
    if uri in _ctx_cache:
        return _ctx_cache[uri]
    typ = ctx.get("type"); cid = uri.split(":")[-1] if uri else ""
    name = None
    try:
        if typ == "playlist" and cid:
            d, _ = _sp_api("GET", "/playlists/%s?fields=name" % cid); name = (d or {}).get("name")
        elif typ == "album" and cid:
            d, _ = _sp_api("GET", "/albums/%s" % cid); name = (d or {}).get("name")
        elif typ == "artist" and cid:
            d, _ = _sp_api("GET", "/artists/%s" % cid); name = (d or {}).get("name")
    except Exception:
        pass
    _ctx_cache[uri] = name
    return name

def _is_liked(tid):
    if not tid:
        return False
    if _sp.get("liked_id") == tid:
        return _sp.get("liked", False)
    d, c = _sp_api("GET", "/me/tracks/contains?ids=" + tid)
    liked = bool(d and d[0]) if (c == 200 and isinstance(d, list)) else False
    _sp["liked_id"] = tid; _sp["liked"] = liked
    return liked

def spotify_status():
    if not _sp_conf():
        return {"available": False, "err": "not-configured"}
    data, code = _sp_api("GET", "/me/player")
    if code == 401:
        return {"available": False, "err": "auth"}
    if code == 204 or not data:
        cp, c2 = _sp_api("GET", "/me/player/currently-playing")   # fallback when no "active" device
        if c2 == 200 and cp and cp.get("item"):
            data = cp
        else:
            return {"available": True, "playing": False}
    item = data.get("item") or {}
    imgs = (item.get("album") or {}).get("images") or []
    dev = data.get("device") or {}
    tid = item.get("id")
    _sp["playing"] = bool(data.get("is_playing"))
    _sp["shuffle"] = bool(data.get("shuffle_state"))
    _sp["track_id"] = tid
    return {"available": True, "playing": _sp["playing"],
            "title": item.get("name"),
            "artist": ", ".join(a["name"] for a in item.get("artists", [])) or None,
            "album": (item.get("album") or {}).get("name"),
            "context": _context_name(data.get("context")),
            "shuffle": _sp["shuffle"], "liked": _is_liked(tid),
            "art": imgs[0]["url"] if imgs else None,
            "dur_ms": item.get("duration_ms") or 0,
            "pos_ms": data.get("progress_ms") or 0,
            "volume": dev.get("volume_percent"), "device": dev.get("name")}

def spotify_cmd(c):
    if c == "playpause":
        c = "pause" if _sp["playing"] else "play"
    if c == "shuffle":
        ns = "false" if _sp.get("shuffle") else "true"
        _, code = _sp_api("PUT", "/me/player/shuffle?state=" + ns)
        _sp["shuffle"] = (ns == "true")
        return {"ok": code in (200, 202, 204), "code": code}
    if c == "like":
        tid = _sp.get("track_id")
        if not tid:
            return {"ok": False, "err": "no-track"}
        if _sp.get("liked"):
            _, code = _sp_api("DELETE", "/me/tracks?ids=" + tid); _sp["liked"] = False
        else:
            _, code = _sp_api("PUT", "/me/tracks?ids=" + tid); _sp["liked"] = True
        return {"ok": code in (200, 202, 204), "code": code}
    routes = {"play": ("PUT", "/me/player/play"), "pause": ("PUT", "/me/player/pause"),
              "next": ("POST", "/me/player/next"), "prev": ("POST", "/me/player/previous"),
              "previous": ("POST", "/me/player/previous")}
    if c not in routes:
        return {"ok": False, "err": "bad-cmd"}
    m, p = routes[c]
    _, code = _sp_api(m, p)
    if c in ("play", "pause"):
        _sp["playing"] = (c == "play")
    return {"ok": code in (200, 202, 204), "code": code}

def spotify_debug():
    out = {}
    t = _sp_token()
    out["have_token"] = bool(t)
    if t:
        try:
            r = urllib.request.urlopen(urllib.request.Request("https://api.spotify.com/v1/me",
                headers={"Authorization": "Bearer " + t}), timeout=8)
            out["me_raw"] = {"code": r.status, "body": r.read()[:400].decode()}
        except urllib.error.HTTPError as e:
            out["me_raw"] = {"code": e.code, "body": e.read()[:400].decode()}
        except Exception as e:
            out["me_raw"] = {"err": str(e)}
    me, c = _sp_api("GET", "/me")
    out["account"] = {"code": c, "name": (me or {}).get("display_name"), "product": (me or {}).get("product")}
    dv, c = _sp_api("GET", "/me/devices")
    out["devices"] = {"code": c, "list": [{"name": x.get("name"), "active": x.get("is_active"),
        "type": x.get("type")} for x in (dv or {}).get("devices", [])]}
    pl, c = _sp_api("GET", "/me/player")
    out["player_code"] = c
    cp, c = _sp_api("GET", "/me/player/currently-playing")
    out["currently_playing"] = {"code": c, "track": ((cp or {}).get("item") or {}).get("name")}
    return out

def spotify_vol(v):
    try: v = max(0, min(100, int(v)))
    except Exception: return {"ok": False, "err": "bad-vol"}
    _, code = _sp_api("PUT", "/me/player/volume?volume_percent=%d" % v)
    return {"ok": code in (200, 202, 204), "volume": v, "code": code}


def _qs(path):
    return urllib.parse.parse_qs(urllib.parse.urlparse(path).query)


BIRD_API_URL = os.environ.get("BIRD_API_URL", "http://192.168.1.250:8090/api/detections")
_bird_cache = {"at": 0.0, "payload": None}

def bird():
    """Latest bird detected by the BirdThing (BirdNET) Pi, proxied + cached."""
    now = time.time()
    if _bird_cache["payload"] is not None and now - _bird_cache["at"] < 25:
        return _bird_cache["payload"]
    out = {"ok": False, "name": None, "recent": False, "ago": None}
    try:
        d = json.load(urllib.request.urlopen(BIRD_API_URL, timeout=4))
        rows = d.get("rows") or []
        if rows:
            row = rows[0]
            out["name"] = row.get("com"); out["ok"] = True
            api_now = d.get("now"); tzoff = float(d.get("tzoff") or 0)
            try:
                from datetime import datetime
                ldt = datetime.strptime(row["date"] + " " + row["time"], "%Y-%m-%d %H:%M:%S")
                row_ms = (ldt - datetime(1970, 1, 1)).total_seconds() * 1000
                if api_now is not None:
                    ago = ((float(api_now) + tzoff * 60000) - row_ms) / 1000.0
                    out["ago"] = round(ago); out["recent"] = 0 <= ago < 900
            except Exception:
                pass
    except Exception as e:
        out["error"] = str(e)
    _bird_cache["payload"] = out; _bird_cache["at"] = time.time()
    return out


PHOTO_UA = "WeatherThing/1.0 (nagarajan1295@gmail.com)"
PHOTO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "birdphotos")
_photo_mem = {}    # name -> (bytes, content_type)
_photo_miss = {}   # name -> last-fail epoch (don't hammer Wikipedia on a miss)

def _slug(name):
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_") or "bird"

def bird_photo(name):
    """A real photo of the species, fetched from Wikipedia and cached (memory + disk).
    The Car Thing has no internet of its own — it only reaches this Pi over Bluetooth —
    so the Pi downloads the image and serves the bytes locally. Returns (bytes, ctype)
    or None."""
    name = (name or "").strip()
    if not name:
        return None
    if name in _photo_mem:
        return _photo_mem[name]
    slug = _slug(name)
    try:
        for ext, ct in ((".jpg", "image/jpeg"), (".png", "image/png")):
            fp = os.path.join(PHOTO_DIR, slug + ext)
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    data = (f.read(), ct)
                _photo_mem[name] = data
                return data
    except Exception:
        pass
    if name in _photo_miss and time.time() - _photo_miss[name] < 600:
        return None
    try:
        api = ("https://en.wikipedia.org/w/api.php?action=query&prop=pageimages"
               "&piprop=thumbnail&pithumbsize=500&format=json&redirects=1&titles="
               + urllib.parse.quote(name))
        d = json.load(urllib.request.urlopen(
            urllib.request.Request(api, headers={"User-Agent": PHOTO_UA}), timeout=6))
        src = None
        for pg in d.get("query", {}).get("pages", {}).values():
            src = (pg.get("thumbnail") or {}).get("source")
            if src:
                break
        if not src:
            _photo_miss[name] = time.time(); return None
        r = urllib.request.urlopen(
            urllib.request.Request(src, headers={"User-Agent": PHOTO_UA}), timeout=10)
        body = r.read()
        ct = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
        ext = ".png" if "png" in ct else ".jpg"
        try:
            os.makedirs(PHOTO_DIR, exist_ok=True)
            with open(os.path.join(PHOTO_DIR, slug + ext), "wb") as f:
                f.write(body)
        except Exception:
            pass
        _photo_mem[name] = (body, ct)
        return (body, ct)
    except Exception:
        _photo_miss[name] = time.time()
        return None


# --- ANCS iPhone notifications ---
# Same-origin proxy for the ANCS gateway (BirdThing Pi :8099) so the Car
# Thing's browser can read iPhone notifications; it has no route to that host
# or port itself. Short cache so a 2.5s UI poll can't stampede the gateway.
_ancs_cache = {"t": 0.0, "d": {"ok": False, "linked": False, "items": []}}
ANCS_URL = "http://192.168.1.250:8099/api/notifications"
ANCS_DISMISS_URL = "http://192.168.1.250:8099/api/dismiss"


def notify_clear(qs=""):
    """Write-through 'Clear' from this screen's notification centre - see
    Store.dismiss_from_display() on the gateway. qs empty = clear everything
    currently shown; qs='uid=<n>' clears just that one row."""
    url = ANCS_DISMISS_URL + ("?" + qs if qs else "")
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        _ancs_cache["t"] = 0.0      # force the next poll to see the clear
        return data
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def notify():
    now = time.time()
    if now - _ancs_cache["t"] < 1.0:
        return _ancs_cache["d"]
    try:
        with urllib.request.urlopen(ANCS_URL, timeout=3) as r:
            d = json.loads(r.read().decode())
        _ancs_cache["d"] = d
        _ancs_cache["good"], _ancs_cache["good_t"] = d, now
    except Exception as e:
        # 2026-10-02: a single failed poll used to be answered with an EMPTY item
        # list, which the Car Thing's page read as "every notification is gone" and
        # retracted the pop-up on screen (found live while this Pi's WiFi was
        # degraded: the swipe-down list worked, the pop-up never showed). Keep
        # serving the last good feed for up to 2 minutes instead, flagged stale;
        # only a longer outage reports ok:false (which the page now ignores too).
        good = _ancs_cache.get("good")
        if good and now - _ancs_cache.get("good_t", 0) < 120:
            d = dict(good)
            d["stale_s"] = int(now - _ancs_cache["good_t"])
            _ancs_cache["d"] = d
        else:
            _ancs_cache["d"] = {"ok": False, "linked": False, "items": [],
                                "error": str(e)[:120]}
    _ancs_cache["t"] = now
    return _ancs_cache["d"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, ctype, body, cache=None):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        if cache: self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def _json(self, obj):
        self._send(200, "application/json", json.dumps(obj).encode())
    def do_GET(self):
        p = self.path
        if p == "/" or p.startswith("/index"):
            try:
                with open(HTML, "rb") as f: body = f.read()
                self._send(200, "text/html", body, cache="no-store, must-revalidate")
            except Exception as e:
                self._send(500, "text/plain", str(e).encode())
        elif p.startswith("/api/time"):
            self._json({"now": int(time.time() * 1000), "tzoff": tz_off_min()})
        elif p.startswith("/api/weather/unit"):
            c = load_wconf(); c["unit"] = "F" if _qs(p).get("u", ["C"])[0].upper() == "F" else "C"
            save_wconf(c); self._json(weather())
        elif p.startswith("/api/weather/loc"):
            q = _qs(p); c = load_wconf()
            try:
                c["lat"] = float(q["lat"][0]); c["lon"] = float(q["lon"][0])
                c["place"] = q.get("place", [c["place"]])[0]; save_wconf(c)
            except Exception:
                pass
            self._json(weather())
        elif p.startswith("/api/weather"):
            self._json(weather())
        elif p.startswith("/api/notifyclear"):
            q = p.split("?", 1)
            self._json(notify_clear(q[1] if len(q) > 1 else ""))
        elif p.startswith("/api/notify"):
            self._json(notify())
        elif p.startswith("/api/bird/photo"):
            nm = _qs(p).get("name", [""])[0] or (bird().get("name") or "")
            ph = bird_photo(nm)
            if ph:
                self._send(200, ph[1], ph[0], cache="max-age=604800")
            else:
                self._send(404, "text/plain", b"no photo")
        elif p.startswith("/api/bird"):
            self._json(bird())
        elif p.startswith("/api/geocode"):
            self._json(geocode(_qs(p).get("q", [""])[0]))
        elif p.startswith("/api/spotify/cmd"):
            self._json(spotify_cmd(_qs(p).get("c", [""])[0]))
        elif p.startswith("/api/spotify/vol"):
            self._json(spotify_vol(_qs(p).get("v", ["50"])[0]))
        elif p.startswith("/api/spotify/debug"):
            self._json(spotify_debug())
        elif p.startswith("/api/spotify"):
            self._json(spotify_status())
        elif p.startswith("/assets/"):
            fn = os.path.basename(urllib.parse.urlparse(p).path)
            fp = os.path.join(ASSETS, fn)
            if os.path.exists(fp) and "/" not in fn.replace("..", ""):
                ct = "font/woff2" if fn.endswith(".woff2") else "application/octet-stream"
                with open(fp, "rb") as f:
                    self._send(200, ct, f.read(), cache="max-age=86400")
            else:
                self._send(404, "text/plain", b"no asset")
        else:
            self._send(404, "text/plain", b"not found")

if __name__ == "__main__":
    print("carthing-weatherstation API on :%d" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
