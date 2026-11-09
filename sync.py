#!/usr/bin/env python3
"""Two-way sync between a Spotify playlist and an Apple Music library playlist.

- Reads the Spotify playlist in a headless browser (falls back to the public embed page).
- Writes to Apple Music via the web player's API (music.apple.com), using your
  media-user-token (from the sync browser profile, or the macOS Keychain).
- Songs you add to the Apple Music playlist are added on Spotify (spotify_add.mjs).
- Only removes songs this script added: songs you add yourself in Apple Music stay.

Usage:
  python3 sync.py setup     # store your Apple Music media-user-token in the Keychain
  python3 sync.py           # run a sync
  python3 sync.py --dry-run # show what would change, change nothing
"""

import base64
import difflib
import getpass
import json
import re
import shlex
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text())
STATE_FILE = HERE / "state.json"
KEYCHAIN_SERVICE = "spotify-apple-sync"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
AMP = "https://amp-api.music.apple.com"


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def notify(msg):
    script = f'display notification {json.dumps(msg)} with title "Playlist sync" sound name "Basso"'
    subprocess.run(["osascript", "-e", script], check=False)


class LoginExpired(Exception):
    def __init__(self, site):
        self.site = site  # "apple" or "spotify"
        name = "Apple Music" if site == "apple" else "Spotify"
        super().__init__(f"{name} login expired. Run: node spotify_add.mjs login{' apple' if site == 'apple' else ''}")


ALERTS_FILE = HERE / "alerts.json"


def load_alerts():
    try:
        return json.loads(ALERTS_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {"failures": 0, "last_alert": 0}


def alert_due(alerts, every=6 * 3600):
    if time.time() - alerts.get("last_alert", 0) < every:
        return False
    alerts["last_alert"] = time.time()
    return True


def ask_to_login(site):
    """Non-blocking popup; 'Log in now' opens the login window for that site."""
    name = "Apple Music" if site == "apple" else "Spotify"
    dialog = (f'button returned of (display dialog "Your {name} login for the playlist sync has expired. '
              f'Log in again to keep the playlists in sync." buttons {{"Later", "Log in now"}} '
              f'default button "Log in now" with title "Playlist sync" with icon caution giving up after 3600)')
    login_cmd = f"cd {shlex.quote(str(HERE))} && node spotify_add.mjs login{' apple' if site == 'apple' else ''}"
    subprocess.Popen(
        ["/bin/bash", "-c", f'[ "$(osascript -e {shlex.quote(dialog)})" = "Log in now" ] && {login_cmd}'],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def http(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": UA, **(headers or {})})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return resp.status, raw


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"apple_playlist_id": None, "synced": {}, "unmatched": {}, "dev_token": None}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(STATE_FILE)


# ---------- Spotify (public embed page) ----------

def spotify_tracks(playlist_id):
    """Returns (name, tracks, complete). complete=False means the list may be cut off,
    so the caller must not treat missing songs as removed."""
    try:
        r = subprocess.run(["node", str(HERE / "spotify_add.mjs"), "list", playlist_id],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[-300:])
        tracks = json.loads(r.stdout.strip().splitlines()[-1])["tracks"]
        if not tracks:
            raise RuntimeError("browser returned no tracks")
        return CONFIG["spotify_playlist_name"], tracks, True
    except Exception as e:
        log(f"WARNING: could not read full playlist in browser ({e}); falling back to embed page")
    name, tracks = spotify_embed_tracks(playlist_id)
    return name, tracks, len(tracks) < 100


def spotify_embed_tracks(playlist_id):
    _, raw = http(f"https://open.spotify.com/embed/playlist/{playlist_id}")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw.decode(), re.S)
    if not m:
        raise RuntimeError("Could not read Spotify embed page (layout changed?)")
    entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    tracks = [
        {"uri": t["uri"], "title": t["title"], "artist": t.get("subtitle", ""), "duration": t.get("duration")}
        for t in entity.get("trackList", [])
        if t.get("entityType", "track") == "track"
    ]
    if not tracks:
        raise RuntimeError("Spotify playlist returned no tracks; refusing to sync (would remove everything)")
    if len(tracks) >= 100:
        log(f"WARNING: embed returned {len(tracks)} tracks (capped); skipping removals this run")
    return entity.get("name", "Spotify playlist"), tracks


# ---------- Apple Music (web player API) ----------

def jwt_exp(token):
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)


def fetch_dev_token():
    """The web player ships a public developer token in its main JS bundle."""
    _, html = http("https://music.apple.com/us/browse")
    for path in re.findall(r'src="(/assets/index[^"]*\.js)"', html.decode()):
        _, js = http("https://music.apple.com" + path)
        for tok in re.findall(r"eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+", js.decode(errors="ignore")):
            try:
                if jwt_exp(tok) > time.time():
                    return tok
            except Exception:
                continue
    raise RuntimeError("Could not find Apple Music web player token (site changed?)")


def keychain_get():
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "media-user-token", "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def browser_apple_token():
    """Reads the login token from the sync browser profile (see spotify_add.mjs login apple)."""
    r = subprocess.run(["node", str(HERE / "spotify_add.mjs"), "apple-token"],
                       capture_output=True, text=True, timeout=90)
    return r.stdout.strip() if r.returncode == 0 else None


class Apple:
    def __init__(self, state):
        self.state = state
        self.user_token = browser_apple_token() or keychain_get()
        if not self.user_token:
            raise LoginExpired("apple")
        tok = state.get("dev_token")
        if not tok or jwt_exp(tok) < time.time() + 3600:
            tok = fetch_dev_token()
            state["dev_token"] = tok
        self.dev_token = tok
        self.storefront = self.get("/v1/me/storefront")["data"][0]["id"]

    def req(self, method, path, body=None, retry=True, attempt=0):
        headers = {
            "Authorization": f"Bearer {self.dev_token}",
            "Media-User-Token": self.user_token,
            "Origin": "https://music.apple.com",
            "Referer": "https://music.apple.com/",
        }
        try:
            _, raw = http(AMP + path, method, headers, body)
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 6:
                time.sleep(5 * 2 ** attempt)  # rate limited: back off 5s, 10s, 20s, ...
                return self.req(method, path, body, retry, attempt + 1)
            if e.code == 401 and retry:
                # Developer token rotated: grab a fresh one and retry once.
                self.dev_token = self.state["dev_token"] = fetch_dev_token()
                return self.req(method, path, body, retry=False)
            if e.code in (401, 403):
                raise LoginExpired("apple") from e
            raise RuntimeError(f"Apple Music {method} {path} -> HTTP {e.code}: {e.read()[:300]!r}") from e

    def get(self, path):
        return self.req("GET", path)

    def search(self, term):
        time.sleep(1)  # stay under the search rate limit
        q = urllib.parse.urlencode({"term": term, "types": "songs", "limit": 10})
        res = self.get(f"/v1/catalog/{self.storefront}/search?{q}")
        return res.get("results", {}).get("songs", {}).get("data", [])

    def playlist_exists(self, pid):
        try:
            self.get(f"/v1/me/library/playlists/{pid}")
            return True
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return False
            raise

    def create_playlist(self, name, description):
        res = self.req("POST", "/v1/me/library/playlists", {"attributes": {"name": name, "description": description}})
        return res["data"][0]["id"]

    def add_tracks(self, pid, catalog_ids):
        for i in range(0, len(catalog_ids), 50):
            chunk = catalog_ids[i:i + 50]
            self.req("POST", f"/v1/me/library/playlists/{pid}/tracks",
                     {"data": [{"id": c, "type": "songs"} for c in chunk]})

    def playlist_tracks(self, pid):
        """Returns {catalog_id: {"lib_ids": [...], "title", "artist", "duration"}}."""
        out, path = {}, f"/v1/me/library/playlists/{pid}/tracks?limit=100"
        while path:
            try:
                res = self.get(path)
            except RuntimeError as e:
                if "HTTP 404" in str(e):  # empty playlist
                    break
                raise
            for t in res.get("data", []):
                a = t.get("attributes", {})
                cid = a.get("playParams", {}).get("catalogId")
                if cid:
                    entry = out.setdefault(cid, {"lib_ids": [], "title": a.get("name", ""),
                                                 "artist": a.get("artistName", ""),
                                                 "duration": a.get("durationInMillis")})
                    entry["lib_ids"].append(t["id"])
            path = res.get("next")
        return out

    def remove_track(self, pid, library_id):
        q = urllib.parse.urlencode({"ids[library-songs]": library_id, "mode": "all"})
        self.req("DELETE", f"/v1/me/library/playlists/{pid}/tracks?{q}")


# ---------- Matching ----------

def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\s*[\(\[][^)\]]*(feat|with|remaster|version|edit|mix)[^)\]]*[\)\]]", "", s)
    s = re.sub(r"\s+-\s+.*(remaster|version|edit|mix|live|mono|stereo).*$", "", s)
    return re.sub(r"[^a-z0-9 ]", "", s).strip()


def match(apple, track):
    want_title, want_artist = norm(track["title"]), norm(track["artist"])
    first_artist = norm(re.split(r",|&| feat", track["artist"])[0])
    best, best_score = None, 0.0
    for term in (f"{track['title']} {first_artist}", track["title"]):
        for song in apple.search(term):
            a = song["attributes"]
            title = difflib.SequenceMatcher(None, want_title, norm(a["name"])).ratio()
            artist = difflib.SequenceMatcher(None, want_artist, norm(a["artistName"])).ratio()
            if first_artist and first_artist in norm(a["artistName"]):
                artist = max(artist, 0.9)
            score = 0.6 * title + 0.4 * artist
            if track.get("duration") and a.get("durationInMillis"):
                if abs(track["duration"] - a["durationInMillis"]) < 4000:
                    score += 0.1
            if score > best_score:
                best, best_score = song, score
        if best_score >= 0.85:
            break
    if best and best_score >= 0.75:
        return best["id"], f"{best['attributes']['name']} – {best['attributes']['artistName']}"
    return None, None


# ---------- Main ----------

def setup():
    print("Open https://music.apple.com in your browser (signed in), then open the developer tools:")
    print("  Safari: Develop > Show Web Inspector > Storage > Cookies > music.apple.com")
    print("  Chrome: View > Developer > Developer Tools > Application > Cookies > https://music.apple.com")
    print("Copy the value of the cookie named 'media-user-token'.")
    token = getpass.getpass("Paste media-user-token (hidden): ").strip()
    if not token:
        sys.exit("Nothing entered.")
    subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                    "-a", "media-user-token", "-w", token], check=True)
    print("Saved to Keychain. Now run: python3 sync.py --dry-run")


def push_to_spotify(state, in_playlist, current, dry_run):
    """Songs you added yourself in Apple Music -> add them to the Spotify playlist via the browser."""
    synced = state["synced"]
    failed = state.setdefault("push_failed", {})  # catalog id -> {"label", "at"}
    known = set(synced.values())
    # Never push a song whose title+artist is already on Spotify (guards against Apple
    # reporting a different catalog id than the one we added, which would cause duplicates).
    on_spotify = {(norm(t["title"]), norm(re.split(r",|&", t["artist"])[0])) for t in current.values()}
    retry_after = time.time() - 24 * 3600  # retry songs Spotify couldn't find once a day
    todo = [
        {"key": cid, "title": t["title"], "artist": t["artist"], "duration": t["duration"]}
        for cid, t in in_playlist.items()
        if cid not in known and failed.get(cid, {}).get("at", 0) < retry_after
        and (norm(t["title"]), norm(re.split(r",|&", t["artist"])[0])) not in on_spotify
    ]
    if not todo:
        return 0
    for s in todo:
        log(f"> new in Apple Music, sending to Spotify: {s['title']} – {s['artist']}")
    job = HERE / ".spotify_job.json"
    job.write_text(json.dumps({"playlist_name": CONFIG["spotify_playlist_name"], "dry_run": dry_run, "songs": todo}))
    r = subprocess.run(["node", str(HERE / "spotify_add.mjs"), "add", str(job)],
                       capture_output=True, text=True, timeout=60 + 45 * len(todo))
    job.unlink(missing_ok=True)
    if r.returncode != 0:
        if "Not logged in to Spotify" in r.stderr:
            raise LoginExpired("spotify")
        raise RuntimeError(f"Spotify browser step failed: {(r.stderr or r.stdout).strip()[-300:]}")
    count = 0
    for res in json.loads(r.stdout.strip().splitlines()[-1]):
        cid = res["key"]
        if res["status"] == "added":
            log(f"  {'[dry-run] would add' if dry_run else 'added'} on Spotify: {res['label']}")
            count += 1
            if not dry_run:
                synced[res["uri"]] = cid  # mark as synced so it doesn't bounce back to Apple Music
                # The public embed page lags behind; don't treat it as removed until it shows up there.
                state.setdefault("pending_push", {})[res["uri"]] = time.time()
                failed.pop(cid, None)
        else:
            label = f"{in_playlist[cid]['title']} – {in_playlist[cid]['artist']}"
            log(f"  could not add on Spotify ({res['status']} {res.get('error', '')}): {label}")
            if not dry_run:
                failed[cid] = {"label": label, "at": time.time()}
    if not dry_run:
        save_state(state)
    return count


def sync(dry_run=False):
    state = load_state()
    sp_name, tracks, complete = spotify_tracks(CONFIG["spotify_playlist_id"])
    log(f"Spotify '{sp_name}': {len(tracks)} tracks")
    apple = Apple(state)
    log(f"Apple Music storefront: {apple.storefront}")

    pid = state.get("apple_playlist_id")
    if pid and not apple.playlist_exists(pid):
        log("Apple Music playlist was deleted; recreating it")
        pid, state["synced"] = None, {}
    if not pid:
        name = CONFIG.get("apple_playlist_name") or sp_name
        if dry_run:
            log(f"[dry-run] would create Apple Music playlist '{name}'")
        else:
            pid = apple.create_playlist(name, "Synced from Spotify")
            state["apple_playlist_id"] = pid
            save_state(state)
            log(f"Created Apple Music playlist '{name}' ({pid})")

    current = {t["uri"]: t for t in tracks}
    synced, unmatched = state["synced"], state["unmatched"]

    # Additions (retry previously unmatched songs too)
    to_add, added_labels = [], []
    for uri, t in current.items():
        if uri in synced:
            continue
        cid, label = match(apple, t)
        if cid:
            to_add.append((uri, cid))
            added_labels.append(f"{t['title']} – {t['artist']}  ->  {label}")
            unmatched.pop(uri, None)
        else:
            unmatched[uri] = f"{t['title']} – {t['artist']}"
    for line in added_labels:
        log(f"+ {line}")
    if to_add and not dry_run:
        apple.add_tracks(pid, [cid for _, cid in to_add])
        for uri, cid in to_add:
            synced[uri] = cid
        save_state(state)

    # Removals: only songs this script added that are gone from Spotify
    pending = state.setdefault("pending_push", {})
    for uri in list(pending):
        if uri in current or pending[uri] < time.time() - 24 * 3600:
            del pending[uri]
    gone = [uri for uri in synced if uri not in current and uri not in pending] if complete else []
    failed_removals = []
    in_playlist = apple.playlist_tracks(pid) if pid else {}
    if gone:
        for uri in gone:
            cid = synced[uri]
            log(f"- removed on Spotify: {uri} (Apple catalog id {cid})")
            if dry_run:
                continue
            try:
                for lib_id in in_playlist.get(cid, {}).get("lib_ids", []):
                    apple.remove_track(pid, lib_id)
                in_playlist.pop(cid, None)  # so push_to_spotify doesn't see it as a new song
                del synced[uri]
            except RuntimeError as e:
                failed_removals.append(cid)
                log(f"  could not remove automatically: {e}")
        if not dry_run:
            save_state(state)

    for uri, label in unmatched.items():
        log(f"? no Apple Music match: {label}")
    if not dry_run:
        save_state(state)

    pushed = push_to_spotify(state, in_playlist, current, dry_run)

    summary = f"{len(to_add)} added, {len(gone) - len(failed_removals)} removed"
    if pushed:
        summary += f", {pushed} sent to Spotify"
    if unmatched:
        summary += f", {len(unmatched)} not found on Apple Music"
    log(("[dry-run] " if dry_run else "") + summary)
    if failed_removals:
        notify(f"{len(failed_removals)} song(s) removed on Spotify need removing by hand in Apple Music")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        setup()
        sys.exit()
    dry_run = "--dry-run" in sys.argv
    alerts = load_alerts()
    try:
        sync(dry_run=dry_run)
        alerts["failures"] = 0
    except LoginExpired as e:
        log(f"ERROR: {e}")
        if not dry_run and alert_due(alerts):
            ask_to_login(e.site)
        sys.exit(1)
    except Exception as e:
        log(f"ERROR: {e}")
        alerts["failures"] = alerts.get("failures", 0) + 1
        # Only bother you if it keeps failing (not for a one-off network blip).
        if not dry_run and alerts["failures"] >= 3 and alert_due(alerts):
            notify(f"Sync has failed {alerts['failures']} times in a row: {e}"[:200])
        sys.exit(1)
    finally:
        if not dry_run:
            ALERTS_FILE.write_text(json.dumps(alerts))
