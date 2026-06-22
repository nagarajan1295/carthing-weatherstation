#!/usr/bin/env python3
"""One-time Spotify auth for BirdThing (Authorization Code + PKCE, no client secret).

Usage:  python spotify_auth.py <CLIENT_ID>
        (or set SPOTIFY_CLIENT_ID env var)

Prereq: a Spotify app at https://developer.spotify.com/dashboard with Redirect URI
        EXACTLY  http://127.0.0.1:8888/callback  and the Web API enabled.

Opens your browser, you log in + authorize once, and this writes spotify.json
({"client_id", "refresh_token"}) next to itself. scp that to the Pi's /opt/birdthing/.
"""
import sys, os, json, base64, hashlib, secrets, threading, webbrowser
import urllib.parse, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

CLIENT_ID = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SPOTIFY_CLIENT_ID", "")).strip()
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPE = "user-read-playback-state user-modify-playback-state user-read-currently-playing"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify.json")

if not CLIENT_ID:
    sys.exit("Pass your Spotify Client ID:  python spotify_auth.py <CLIENT_ID>")

verifier = secrets.token_urlsafe(64)[:96]
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
state = secrets.token_urlsafe(16)
auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
    "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT,
    "scope": SCOPE, "code_challenge_method": "S256", "code_challenge": challenge, "state": state})

result = {}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" not in q:
            self.send_response(400); self.end_headers(); return
        if q.get("state", [""])[0] != state:
            self.send_response(400); self.end_headers()
            result["err"] = "state mismatch"; return
        data = urllib.parse.urlencode({
            "grant_type": "authorization_code", "code": q["code"][0],
            "redirect_uri": REDIRECT, "client_id": CLIENT_ID,
            "code_verifier": verifier}).encode()
        try:
            req = urllib.request.Request("https://accounts.spotify.com/api/token", data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            tok = json.load(urllib.request.urlopen(req, timeout=10))
            result["refresh_token"] = tok["refresh_token"]
        except urllib.error.HTTPError as e:
            result["err"] = e.read().decode()
        except Exception as e:
            result["err"] = str(e)
        msg = ("BirdThing: Spotify connected. You can close this tab."
               if "refresh_token" in result else "Auth failed: " + result.get("err", "?"))
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(("<html><body style='font-family:sans-serif;padding:40px'>"
                          "<h2>" + msg + "</h2></body></html>").encode())
        threading.Thread(target=self.server.shutdown, daemon=True).start()

print("Opening browser to authorize... (log in + click Agree)")
print("If it doesn't open, paste this URL:\n" + auth_url + "\n")
webbrowser.open(auth_url)
srv = HTTPServer(("127.0.0.1", 8888), Handler)
srv.serve_forever()

if "refresh_token" not in result:
    sys.exit("FAILED: " + result.get("err", "no token"))
json.dump({"client_id": CLIENT_ID, "refresh_token": result["refresh_token"]}, open(OUT, "w"))
print("OK -> wrote " + OUT)
