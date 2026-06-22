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

# WMO weather code -> (emoji icon, short description)
WMO = {0:("☀️","Clear"),1:("\U0001f324️","Mainly clear"),2:("⛅","Partly cloudy"),
 3:("☁️","Overcast"),45:("\U0001f32b️","Fog"),48:("\U0001f32b️","Rime fog"),
 51:("\U0001f326️","Light drizzle"),53:("\U0001f326️","Drizzle"),55:("\U0001f326️","Heavy drizzle"),
 56:("\U0001f327️","Freezing drizzle"),57:("\U0001f327️","Freezing drizzle"),
 61:("\U0001f327️","Light rain"),63:("\U0001f327️","Rain"),65:("\U0001f327️","Heavy rain"),
 66:("\U0001f327️","Freezing rain"),67:("\U0001f327️","Freezing rain"),
 71:("❄️","Light snow"),73:("❄️","Snow"),75:("❄️","Heavy snow"),77:("❄️","Snow grains"),
 80:("\U0001f326️","Showers"),81:("\U0001f327️","Showers"),82:("\U0001f327️","Heavy showers"),
 85:("\U0001f328️","Snow showers"),86:("\U0001f328️","Snow showers"),
 95:("⛈️","Thunderstorm"),96:("⛈️","Thunderstorm"),99:("⛈️","Thunderstorm")}
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
    return WMO.get(int(code), ("\U0001f321️", "—"))

def weather():
    c = load_wconf()
    imperial = c["unit"] == "F"
    base = {"unit": c["unit"], "place": c["place"],
            "now": int(time.time() * 1000), "tzoff": tz_off_min()}
    try:
        url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
               "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
               "weather_code,wind_speed_10m"
               "&hourly=temperature_2m,weather_code"
               "&daily=weather_code,temperature_2m_max,temperature_2m_min"
               "&forecast_days=7&timezone=auto"
               "&temperature_unit=%s&wind_speed_unit=%s"
               % (c["lat"], c["lon"], "fahrenheit" if imperial else "celsius",
                  "mph" if imperial else "kmh"))
        d = json.load(urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": "carthing-weatherstation/1.0"}),
            timeout=8))
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
                     "hourly": hourly, "daily": daily})
        return base
    except Exception as e:
        base.update({"temp": None, "icon": "\U0001f321️", "desc": "—",
                     "hourly": [], "daily": [], "err": str(e)})
        return base

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

def spotify_status():
    if not _sp_conf():
        return {"available": False, "err": "not-configured"}
    data, code = _sp_api("GET", "/me/player")
    if code == 401:
        return {"available": False, "err": "auth"}
    if code == 204 or not data:
        return {"available": True, "playing": False}
    item = data.get("item") or {}
    imgs = (item.get("album") or {}).get("images") or []
    dev = data.get("device") or {}
    _sp["playing"] = bool(data.get("is_playing"))
    return {"available": True, "playing": _sp["playing"],
            "title": item.get("name"),
            "artist": ", ".join(a["name"] for a in item.get("artists", [])) or None,
            "album": (item.get("album") or {}).get("name"),
            "art": imgs[0]["url"] if imgs else None,
            "dur_ms": item.get("duration_ms") or 0,
            "pos_ms": data.get("progress_ms") or 0,
            "volume": dev.get("volume_percent"), "device": dev.get("name")}

def spotify_cmd(c):
    if c == "playpause":
        c = "pause" if _sp["playing"] else "play"
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

def spotify_vol(v):
    try: v = max(0, min(100, int(v)))
    except Exception: return {"ok": False, "err": "bad-vol"}
    _, code = _sp_api("PUT", "/me/player/volume?volume_percent=%d" % v)
    return {"ok": code in (200, 202, 204), "volume": v, "code": code}


def _qs(path):
    return urllib.parse.parse_qs(urllib.parse.urlparse(path).query)

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
        elif p.startswith("/api/geocode"):
            self._json(geocode(_qs(p).get("q", [""])[0]))
        elif p.startswith("/api/spotify/cmd"):
            self._json(spotify_cmd(_qs(p).get("c", [""])[0]))
        elif p.startswith("/api/spotify/vol"):
            self._json(spotify_vol(_qs(p).get("v", ["50"])[0]))
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
