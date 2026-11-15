# spotify-recent

Put the Spotify tracks you played in a date or time range into a playlist.

```
./spotify-recent-to-playlist.py --from 2026-11-07 --dry-run                    # list what would be added
./spotify-recent-to-playlist.py --from 2026-11-07 --name "Weekend"             # create a playlist
./spotify-recent-to-playlist.py --from 2026-11-07 --update <playlist URL>      # replace a playlist's contents
./spotify-recent-to-playlist.py --source api --hours 3                         # last 3 hours, exact times
```

Options: `--from`, `--to` (default: now), `--hours`, `--name`, `--update`, `--public`,
`--keep-duplicates`, `--dry-run`, `--source recents|api`.

## Sources

| | `--source recents` (default) | `--source api` |
|---|---|---|
| Reaches back | weeks | last 50 plays only |
| Time resolution | date only | exact timestamp |
| Short or skipped plays | included | only plays of about 30 s or more |
| Needs | a logged-in Chrome reachable through `fab-chrome-fetch` | only the Spotify app credentials |

- **api** uses the Web API endpoint `/me/player/recently-played`. Spotify only returns the last
  50 plays, so anything older is invisible to it, and today's plays can lag behind.
- **recents** reads the data behind the web player's Recents page (open.spotify.com/recents).
  It replays the page's own `recents` request with growing offsets from inside a logged-in
  browser tab, so it reaches back weeks. It depends on `fab-chrome-fetch`, a private helper
  that drives a real Chrome through the Playwright MCP extension; set `FAB_CHROME_FETCH` to its
  path. Without it, use `--source api`.
- Podcast episodes are skipped. Entries for playlists this tool generated (`Played since …`,
  `Played 2026-…`) and the `--update` target are not counted as listening.
- For exact times of older plays, request "Extended streaming history" under
  spotify.com/account/privacy.

## Setup

1. Install [uv](https://docs.astral.sh/uv/). The script declares its dependency (`spotipy`)
   inline, so `uv` installs it on the first run.
2. Create an app at https://developer.spotify.com/dashboard (Web API) and add the redirect URI
   `http://127.0.0.1:8888/callback` exactly. Spotify rejects `localhost`.
3. Store its credentials in the macOS Keychain:
   ```
   security add-generic-password -s spotify-client-id -a "$USER" -w 'CLIENT_ID'
   security add-generic-password -s spotify-client-secret -a "$USER" -w 'CLIENT_SECRET'
   ```
   Or set `SPOTIPY_CLIENT_ID` and `SPOTIPY_CLIENT_SECRET`.
4. Run any command. The first run opens a browser for the Spotify login and caches the token
   in `~/.cache/spotify-recent-token.json`.
