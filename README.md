# Spotify ⇄ Apple Music playlist sync

Keeps one Spotify playlist and one Apple Music playlist in sync, in both directions, on a Mac.
Built for a shared playlist where one person listens on Spotify and the other on Apple Music:
whoever adds a song, it shows up on the other side within the hour.

- **Spotify → Apple Music:** new songs are matched in the Apple Music catalog (fuzzy title and artist
  match, duration as a tie-breaker) and added to the playlist. Songs removed on Spotify are removed again,
  but only songs this tool added; anything you add by hand in Apple Music stays.
- **Apple Music → Spotify:** songs added to the Apple Music playlist are searched on open.spotify.com and
  added through the web player, so a free Spotify account works (no Web API or Premium needed).
- Runs every hour from `launchd`, logs to `sync.log`, and only bothers you with a notification when
  something keeps failing or a login has expired (with a one-click "Log in now").

It does not use official APIs on either side: it drives the Spotify web player with Playwright and calls the
Apple Music web player's API with your own login. It is an unofficial personal tool; either site can change and
break it, and you use it at your own risk and under each service's terms.

## Setup

Requires macOS, Python 3, Node.js and Google Chrome.

```bash
npm install                          # Playwright
cp config.example.json config.json   # then fill in the playlist ID and names
node spotify_add.mjs login           # log in to Spotify in the Chrome window, then quit it (Cmd+Q)
node spotify_add.mjs login apple     # same for music.apple.com
python3 sync.py --dry-run            # shows what would change, changes nothing
python3 sync.py                      # first real sync (creates the Apple Music playlist if needed)
./install.sh                         # run every hour and at login
```

The Spotify playlist ID is the part after `/playlist/` in the share link. Logins live in a separate Chrome
profile inside the project folder (`.spotify-profile/`, git-ignored). As an alternative for Apple Music,
`python3 sync.py setup` stores your `media-user-token` in the macOS Keychain.

## How it stays safe

- It never removes a song it did not add, and it skips all removals when it could not read the full Spotify
  playlist (the public embed page stops at 100 songs).
- A song pushed to Spotify is marked as synced so it does not bounce back, and a title and artist already on
  Spotify is never pushed twice.
- Apple Music rate limits are handled with exponential back-off; an expired developer token is refreshed and
  the request retried once.

## Files

| File | What it is |
|---|---|
| `sync.py` | The sync: matching, Apple Music API calls, state, alerts |
| `spotify_add.mjs` | Playwright helper: Spotify login, read the full playlist, add songs, read the Apple login |
| `install.sh` | Installs the hourly `launchd` job |
| `config.json` | Your playlist ID and names (not in git; see `config.example.json`) |
| `state.json`, `sync.log`, `alerts.json` | Local state and log (not in git) |

Built with Claude Code. MIT licence.
