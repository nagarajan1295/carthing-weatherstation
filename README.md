# Car Thing Weather Station

Turn a [Spotify Car Thing](https://carthing.spotify.com) into a desk **weather station**:
a big clock, current conditions + a 7-day / hourly forecast, and a **Spotify now-playing
remote** — all on the 800×480 screen.

It's the standalone, weather-first sibling of [BirdThing](https://github.com/nagarajan1295).
No database, no microphone — just weather, time, and music.

![layout: clock + current weather on top, hourly forecast strip below](docs/screenshot.png)

## How it works

- The Car Thing runs the **[Nocturne](https://github.com/usenocturne/nocturne) 4.x** firmware as a
  Chromium kiosk and links to a host (Raspberry Pi or any Linux box) over **Bluetooth-PAN** (or USB).
- A tiny Python server (`server/weather_api.py`, port **8095**) serves the dashboard and three APIs:
  - **Weather / forecast** — free [Open-Meteo](https://open-meteo.com) (no API key).
  - **Clock** — the host's NTP time + timezone (the Car Thing has no RTC, so the page never trusts
    the browser clock).
  - **Spotify** — the Spotify **Web API** as a *remote*: it shows whatever is playing on **your own
    device** (phone, Alexa, computer) and sends play/pause/skip/volume to it. Audio never plays on the
    Car Thing or the host. Requires **Spotify Premium**.

## Setup

### 1. Server
```sh
sudo mkdir -p /opt/weatherstation
sudo cp -r server/* /opt/weatherstation/
sudo cp server/weatherstation-api.service /etc/systemd/system/
# edit User= in the unit to match the owner of /opt/weatherstation
sudo systemctl enable --now weatherstation-api
```
Check: `curl localhost:8095/api/weather`.

### 2. Point the Car Thing at it
In your Nocturne build, set the kiosk URL to `http://<host-ip>:8095/`
(e.g. the `start-chromium` launcher / `NOCTURNE_CHROMIUM_URL`). Over Bluetooth-PAN that's typically
`http://192.168.44.1:8095/`.

### 3. Spotify (optional)
1. Create an app at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard)
   (enable **Web API**), Redirect URI **exactly** `http://127.0.0.1:8888/callback`. Copy the **Client ID**.
2. On any machine with a browser:
   ```sh
   python spotify_auth.py <CLIENT_ID>
   ```
   Log in + Agree once. It writes `spotify.json`.
3. Copy it to the server: `scp spotify.json <host>:/opt/weatherstation/` then restart the service.

### 4. Weather location
Defaults to Potsdam, NY. Change it on-device: **Settings → Location**, or
`curl 'localhost:8095/api/weather/loc?lat=..&lon=..&place=City'`.

## Controls

| Input | Action |
|-------|--------|
| Top buttons 1–4 | Home · Forecast · Spotify · Settings |
| Knob (Spotify) | ← / → skip · press = play/pause · up/down = volume |
| Knob (Settings) | scroll options · press = toggle / activate slider |
| `m` button | screen on/off |
| Touch | tap conditions → forecast, tap now-playing chip → Spotify |

Brightness is driven by an optional Car Thing-side control server at `127.0.0.1:8091`
(`/bright?level=0-100`, `/display?on=0|1`) — the same one BirdThing uses.

## API

| Route | Returns |
|-------|---------|
| `GET /api/weather` | current + `hourly` + `daily` + `now`/`tzoff` |
| `GET /api/weather/unit?u=C\|F` | switch units |
| `GET /api/weather/loc?lat=&lon=&place=` | set location |
| `GET /api/geocode?q=` | city search |
| `GET /api/time` | `{now, tzoff}` |
| `GET /api/spotify` | now-playing |
| `GET /api/spotify/cmd?c=playpause\|play\|pause\|next\|prev` | transport |
| `GET /api/spotify/vol?v=0-100` | volume |

## License

MIT.
