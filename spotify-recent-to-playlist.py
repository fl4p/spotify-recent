#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["spotipy>=2.24"]
# ///
"""Put the Spotify tracks you played in a time range into a playlist.

Two sources:
  recents (default)  Spotify's own "Recents" feed (open.spotify.com/recents), read through your
                     logged-in Chrome via fab-chrome-fetch ($FAB_CHROME_FETCH, default
                     ~/dev/bin/fab-chrome-fetch). Goes back weeks, includes short
                     and skipped plays, but has DATES ONLY (no times); order within a day follows
                     Spotify's grouping (playlist/radio/album), oldest group first.
  api                Web API /me/player/recently-played: exact timestamps, only plays of ~30 s or
                     more, but ONLY THE LAST 50 PLAYS. Needs no browser.

Credentials: Keychain items `spotify-client-id` / `spotify-client-secret` (or env
SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET). Redirect URI registered in the Spotify app:
http://127.0.0.1:8888/callback. Token cache: ~/.cache/spotify-recent-token.json.
When run by an agent, run it outside the sandbox (Keychain, Chrome extension relay).

Examples:
  spotify-recent-to-playlist.py --from 2026-11-07 --dry-run
  spotify-recent-to-playlist.py --from 2026-11-07 --to 2026-11-09 --name "Weekend"
  spotify-recent-to-playlist.py --from 2026-11-07 --update https://open.spotify.com/playlist/<id>
  spotify-recent-to-playlist.py --source api --hours 3
"""
import argparse
import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyOAuth

FCF = os.path.expanduser(os.environ.get("FAB_CHROME_FETCH", "~/dev/bin/fab-chrome-fetch"))
RECENTS_URL = "https://open.spotify.com/recents"
SCOPE = "user-read-recently-played playlist-modify-private playlist-modify-public"   # changing it forces a browser re-login
OWN_PLAYLIST_RE = re.compile(r"^Played (since |\d{4}-)")   # names this tool generates; their "plays" are not history


# ------------------------------------------------------------------------------------ auth
def secret(env, kc):
    v = os.environ.get(env)
    if v:
        return v
    r = subprocess.run(["security", "find-generic-password", "-s", kc, "-w"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"missing credential: set {env} or Keychain item '{kc}' "
                         "(outside the sandbox the Keychain is readable)")
    return r.stdout.strip()


def client():
    return spotipy.Spotify(auth_manager=SpotifyOAuth(
        client_id=secret("SPOTIPY_CLIENT_ID", "spotify-client-id"),
        client_secret=secret("SPOTIPY_CLIENT_SECRET", "spotify-client-secret"),
        redirect_uri="http://127.0.0.1:8888/callback",
        scope=SCOPE,
        cache_path=str(Path.home() / ".cache" / "spotify-recent-token.json"),
    ))


# ------------------------------------------------------------------------------- source: api
def from_api(sp, start, end):
    """[(datetime, uri, label)] oldest first, plus a warning string or None."""
    items = sp.current_user_recently_played(limit=50)["items"]
    if not items:
        raise SystemExit("API returned no recently played tracks")
    out = []
    for it in items:
        t = dt.datetime.fromisoformat(it["played_at"].replace("Z", "+00:00")).astimezone()
        tr = it["track"]
        if tr and tr.get("uri", "").startswith("spotify:track:") and start <= t < end:
            out.append((t, tr["uri"], f"{', '.join(a['name'] for a in tr['artists'])} - {tr['name']}"))
    oldest = min(dt.datetime.fromisoformat(i["played_at"].replace("Z", "+00:00")) for i in items).astimezone()
    warn = None
    if start < oldest and len(items) == 50:
        warn = (f"range starts before the oldest play the API returns ({oldest:%Y-%m-%d %H:%M}); "
                "earlier plays are missing -- use --source recents")
    out.sort(key=lambda x: x[0])
    return out, warn


# --------------------------------------------------------------------------- source: recents
# Replaces fab-chrome-fetch's NAV/READ templates: record the page's own `recents` pathfinder
# request, then replay it from inside the page with growing offsets until the feed is older
# than MINDATE. Read-only, no clicks. Auth headers are used in-page and never returned.
# Why not scroll: the tab runs in the background, so the virtualized list never renders rows.
NAV_JS = """/*fcf:nav*/ async (page) => {
  %FIND%
  p.__reqs = [];
  p.on('request', r => {
    try {
      if (r.method() !== 'POST' || !/pathfinder/.test(r.url())) return;
      const b = r.postData() || '';
      if (/"operationName":"recents"/.test(b)) p.__reqs.push({url: r.url(), headers: r.headers(), body: b});
    } catch (e) {}
  });
  let err = null;
  try { await p.goto(%URL%, {waitUntil: 'domcontentloaded', timeout: %T%}); }
  catch (e) { err = String((e && e.message) || e).split('\\n')[0].slice(0, 300); }
  return {ok: true, err: err, url: p.url()};
}"""

READ_JS = """/*fcf:read*/ async (page) => {
  %FIND%
  try {
    for (let i = 0; i < 25 && !(p.__reqs || []).length; i++) await p.waitForTimeout(1000);
    const pick = (p.__reqs || [])[0];
    if (!pick) return {title: 'no-recents-request', text: JSON.stringify({error: 'the page sent no recents request (logged out? layout change?)', pages: []}), markers: [], html: null, url: p.url()};
    const body0 = JSON.parse(pick.body);
    const keep = ['authorization','client-token','content-type','accept','app-platform','spotify-app-version','accept-language'];
    const hdr = {}; for (const k of Object.keys(pick.headers)) if (keep.includes(k.toLowerCase())) hdr[k] = pick.headers[k];
    const setOffset = (o, v) => { let n = 0; for (const k of Object.keys(o)) { if (/^offset$/i.test(k)) { o[k] = v; n++; } else if (o[k] && typeof o[k] === 'object') n += setOffset(o[k], v); } return n; };
    const pages = [];
    for (let off = 0; off <= %MAXOFF%; off += 100) {
      const body = JSON.parse(JSON.stringify(body0));
      if (!setOffset(body.variables, off)) return {title: 'x', text: JSON.stringify({error: 'recents request has no offset variable', pages}), markers: [], html: null, url: p.url()};
      const res = await p.evaluate(async ([u, h, b]) => {
        const r = await fetch(u, {method: 'POST', headers: h, body: b, credentials: 'include'});
        return {status: r.status, text: await r.text()};
      }, [pick.url, hdr, JSON.stringify(body)]);
      pages.push({off, status: res.status, body: res.text.slice(0, 4000000)});
      if (res.status !== 200) break;
      const ds = [...res.text.matchAll(/"addedAt":\\{"day":(\\d+),"month":(\\d+),"year":(\\d+)\\}/g)].map(m => +m[3]*10000 + +m[2]*100 + +m[1]);
      if (!ds.length || Math.min(...ds) < %MINDATE%) break;
      await p.waitForTimeout(800);
    }
    return {title: String(await p.title()), text: JSON.stringify({pages}), markers: [], html: null, url: p.url()};
  } catch (e) {
    return {transient: String((e && e.message) || e).split('\\n')[0].slice(0, 200)};
  }
}"""


def _osa(script, *args):
    return subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True, timeout=30)


COUNT_RECENTS_OSA = f'''set n to 0
tell application "Google Chrome"
  repeat with w in windows
    repeat with t in tabs of w
      if URL of t is "{RECENTS_URL}" then set n to n + 1
    end repeat
  end repeat
end tell
return n'''


def count_recents_tabs():
    r = _osa(COUNT_RECENTS_OSA)
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


def cleanup_left_open(before):
    """fab-chrome-fetch refuses to close its tab when another extension client is running.
    Close (a) surplus Recents tabs beyond the pre-run count, newest-indexed first, and
    (b) extension connect pages for fab-chrome-fetch whose relay port has no listener (dead)."""
    after = count_recents_tabs()
    closed = 0
    if before is not None and after is not None and after > before:
        r = _osa(f'''set k to {after - before}
set n to 0
tell application "Google Chrome"
  repeat with w in (reverse of (windows as list))
    repeat with i from (count of tabs of w) to 1 by -1
      if n < k and URL of tab i of w is "{RECENTS_URL}" then
        close tab i of w
        set n to n + 1
      end if
    end repeat
  end repeat
end tell
return n''')
        closed += int(r.stdout.strip() or 0) if r.returncode == 0 else 0
    r = _osa('''set out to ""
tell application "Google Chrome"
  repeat with w in windows
    repeat with t in tabs of w
      set u to URL of t
      if u contains "/connect.html?mcpRelayUrl=" and u contains "%22fab-chrome-fetch%22" then set out to out & u & linefeed
    end repeat
  end repeat
end tell
return out''')
    for u in (r.stdout.split() if r.returncode == 0 else []):
        m = re.search(r"%3A(\d+)%2Fextension", u)
        if not m:
            continue
        port = m.group(1)
        live = subprocess.run(["lsof", "-nP", "-iTCP:" + port, "-sTCP:LISTEN", "-t"], capture_output=True, text=True).stdout.strip()
        if live:
            continue   # someone's live relay -- not ours to close
        rr = _osa(f'''set n to 0
tell application "Google Chrome"
  repeat with w in windows
    repeat with i from (count of tabs of w) to 1 by -1
      if URL of tab i of w contains "%3A{port}%2Fextension" then
        close tab i of w
        set n to n + 1
      end if
    end repeat
  end repeat
end tell
return n''')
        closed += int(rr.stdout.strip() or 0) if rr.returncode == 0 else 0
    return closed


def fetch_recents_pages(min_date, max_offset=3000):
    loader = importlib.machinery.SourceFileLoader("fcf", FCF)
    spec = importlib.util.spec_from_loader("fcf", loader)
    fcf = importlib.util.module_from_spec(spec)
    loader.exec_module(fcf)
    fcf.NAV_JS = NAV_JS
    fcf.READ_JS = READ_JS.replace("%MINDATE%", str(min_date)).replace("%MAXOFF%", str(max_offset))
    out = tempfile.mkdtemp(prefix="spotify-recents-")
    before = count_recents_tabs()
    rc = fcf.main(["--wait", "240", "--out", out, RECENTS_URL])
    log = Path(out, "fab-chrome-fetch.log").read_text(errors="replace")
    if "LEFT-OPEN" in log or "LEFT OPEN" in log:
        n = cleanup_left_open(before)
        print(f"note: fab-chrome-fetch left a tab open (another extension client was active); closed {n} leftover tab(s)",
              file=sys.stderr)
    if rc == 4:
        raise SystemExit("another Chrome-extension client is running (fab-chrome-fetch exit 4); retry in a minute")
    txt = next(Path(out).glob("*.txt"), None)
    if txt is None:
        raise SystemExit(f"fab-chrome-fetch produced no output (exit {rc}); log: {out}/fab-chrome-fetch.log")
    data = json.loads(txt.read_text())
    if data.get("error"):
        raise SystemExit("recents: " + data["error"])
    bad = [p for p in data["pages"] if p["status"] != 200]
    if bad:
        raise SystemExit(f"recents: HTTP {bad[0]['status']} at offset {bad[0]['off']}: {bad[0]['body'][:200]}")
    return data["pages"]


def from_recents(pages, d_from, d_to, exclude_context=None):
    """Parse the recents feed. Returns ([(date_int, uri, label)] oldest first, oldest_date_int).
    Keeps only tracks inside 'played' groups (not 'saved' = added-to-playlist entries), skips
    podcast episodes and plays of `exclude_context` (the target playlist itself)."""
    groups, rows, oldest = {}, [], None
    for pg in pages:
        for o in json.loads(pg["body"])["data"]["lists"][0]["items"]["items"]:
            a = o["addedAt"]
            date = a["year"] * 10000 + a["month"] * 100 + a["day"]
            oldest = date if oldest is None else min(oldest, date)
            attrs = {x["key"]: x["value"] for x in o.get("formatListAttributes", [])}
            ent = o.get("entity") or {}
            e = ent.get("data") or {}
            it = e.get("identityTrait") or {}
            if "children_group_id" in attrs:
                groups[attrs["children_group_id"]] = ("recent_type_played" in attrs, ent.get("_uri"), it.get("name"))
                continue
            gid = next((k.split("_")[-1] for k in attrs if k.startswith("group_id_")), None)
            who = ", ".join(c["name"] for c in ((it.get("contributors") or {}).get("items") or []))
            rows.append((date, gid, ent.get("_uri", ""), f"{who} - {it.get('name')}"))
    sel = []
    for date, gid, uri, label in rows:
        g = groups.get(gid)
        if not g or not g[0] or not uri.startswith("spotify:track:"):
            continue
        if (exclude_context and g[1] == exclude_context) or OWN_PLAYLIST_RE.match(g[2] or ""):
            continue
        if d_from <= date <= d_to:
            sel.append((date, uri, label))
    sel.reverse()   # the feed is newest first
    return sel, oldest


# ------------------------------------------------------------------------------------- main
def parse_when(s):
    return dt.datetime.fromisoformat(s).astimezone()   # naive -> local tz


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 epilog=__doc__.split("\n\n", 1)[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=("recents", "api"), default="recents")
    ap.add_argument("--from", dest="start", type=parse_when, help="local date/time; recents uses the date only")
    ap.add_argument("--to", dest="end", type=parse_when, help="local date/time (default: now); recents: inclusive date")
    ap.add_argument("--hours", type=float, help="alternative to --from: last N hours")
    ap.add_argument("--name", help="name for a new playlist")
    ap.add_argument("--update", metavar="PLAYLIST", help="replace the contents of this playlist (URL/URI/id) instead of creating one")
    ap.add_argument("--public", action="store_true")
    ap.add_argument("--keep-duplicates", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    end = a.end or dt.datetime.now().astimezone()
    if a.hours:
        start = end - dt.timedelta(hours=a.hours)
    elif a.start:
        start = a.start
    else:
        ap.error("give --from or --hours")

    sp = client()
    target = None
    if a.update:
        m = re.search(r"(?:playlist[/:])?([A-Za-z0-9]{22})", a.update)
        if not m:
            ap.error("--update: not a playlist URL/URI/id")
        target = m.group(1)

    if a.source == "api":
        rows, warn = from_api(sp, start, end)
        fmt = lambda r: f"{r[0]:%a %m-%d %H:%M}"
    else:
        d_from = int(f"{start:%Y%m%d}")
        d_to = int(f"{end:%Y%m%d}")
        pages = fetch_recents_pages(d_from)
        rows, oldest = from_recents(pages, d_from, d_to, exclude_context=f"spotify:playlist:{target}" if target else None)
        warn = None
        if oldest is None or oldest >= d_from:
            warn = f"the Recents feed ends at {oldest}; it may not reach back to {d_from}"
        fmt = lambda r: f"{str(r[0])[4:6]}-{str(r[0])[6:]}"
        if start.time() != dt.time(0) or (a.end and a.end.time() != dt.time(0)):
            print("note: --source recents has dates only; times in --from/--to are ignored", file=sys.stderr)

    if warn:
        print("WARNING: " + warn, file=sys.stderr)
    uris, seen = [], set()
    for r in rows:
        if not a.keep_duplicates and r[1] in seen:
            continue
        seen.add(r[1])
        uris.append(r[1])
        print(f"{fmt(r)}  {r[2]}")
    print(f"{len(uris)} tracks ({a.source}) {start:%Y-%m-%d %H:%M} .. {end:%Y-%m-%d %H:%M}")
    if a.dry_run or not uris:
        return

    if target:
        pl = sp.playlist(target, fields="id,name,external_urls")
        sp.playlist_replace_items(target, uris[:100])
        for i in range(100, len(uris), 100):
            sp.playlist_add_items(target, uris[i:i + 100])
        total = sp.playlist_items(target, limit=1)["total"]
        print(f"Updated '{pl['name']}' ({total} tracks): {pl['external_urls']['spotify']}")
    else:
        name = a.name or (f"Played {start:%Y-%m-%d}–{end:%Y-%m-%d}" if a.source == "recents"
                          else f"Played {start:%Y-%m-%d %H:%M}–{end:%H:%M}")
        pl = sp.current_user_playlist_create(name, public=a.public)
        for i in range(0, len(uris), 100):
            sp.playlist_add_items(pl["id"], uris[i:i + 100])
        print(f"Created '{name}' ({len(uris)} tracks): {pl['external_urls']['spotify']}")


if __name__ == "__main__":
    main()
