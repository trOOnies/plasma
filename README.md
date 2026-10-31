<p align="center"><img src="src/plasma/static/plasma.svg" alt="" width="128" height="128"></p>

# plasma

Personal tracker for TV series, movies and games. A FastAPI app with server-rendered pages, backed by a local SQLite file.

- Track movies and series as planned, watching, completed or dropped, with notes and a rating of 1 to 5 stars, which episodes can have too. Titles start with no status.
- Link series, movies and games into a franchise, and step through it in release order.
- Track games too, with the hours you've played and whether you want or own each one. Singleplayer and multiplayer each keep their own status, and multiplayer can't be completed.
- Tag titles with genres and an age restriction, then filter the library by either.
- Group series, movies and games into plans, like playlists. Built-in plans hold everything you've marked with a heart (Favorites) or as planned (Planned).
- Add seasons with New season, change them with the pencil icon, and tick off episodes one at a time or by season. A series' status follows its progress.
- See what to watch next on the home page, and search, filter and page through the library.
- Keep a duration, release date and description for every movie and episode.
- See what's upcoming and what just came out on the home page.
- Keep one central list of platforms, split into Streaming Platforms and Game Consoles, and record where each title can be watched or played.
- Mark favorites, which go to the top of their tab, and show an image on each title's and platform's page.
- Load your titles from YAML files in `info/`.

The app's Help tab explains how to use it.

## Setup

Requires Python 3.10+.

```sh
python3 -m venv .venv
source .venv/bin/activate
make install
```

## Usage

```sh
make import   # Load your files from info/
make serve    # Run the app on http://127.0.0.1:8000
```

| Command | What it does |
| --- | --- |
| `make serve` | Run the web app, reloading when the code changes |
| `make import` | Import movies and series from `info/` |
| `make seed` | Fill the library with demo data from `src/plasma/demo.yaml` |
| `make test` | Run the pytest suite with coverage |
| `make lint` | Run ruff and apply its fixes |
| `make build` | Build the package |

To stop the server, click the door button in the top right corner of the app, or press Ctrl+C.

The app has no login, so keep it bound to `127.0.0.1` (the default).

## Your data

Both of these folders are gitignored:

- `info/` holds your input files.
- `local/plasma.db` is the database. Set `PLASMA_DB` to keep it somewhere else.

### Files in `info/`

The app needs this layout:

```
info/
  movie.yaml
  series.yaml
  game.yaml
  platform.yaml
  images/
    movie/
    series/
    game/
    platform/
```

If any of these is missing, `make serve` and `make import` create it empty. `make serve` then stops with an error explaining why, so add your titles, run `make import` and start it again. An empty YAML file is valid, so the next `make serve` starts normally.

Every `.yaml` or `.yml` file in `info/` is imported, and each one can hold any of the `streaming_service`, `game_console`, `movie`, `series` and `game` lists:

```yaml
# platform.yaml
streaming_service:
  - name: Netflix                  # Required.
    url: https://www.netflix.com   # Optional. Must start with http:// or https://.
    image: netflix.png             # A file in info/images/platform/.
game_console:
  - name: PlayStation 5
    release_year: 2020             # Game consoles only.

# movie.yaml
movie:
  - name: Arrival          # Required.
    year: 2016
    producer: FilmNation Entertainment
    status: completed      # planned, watching, completed or dropped. Blank by default.
    rating: 5              # 1 to 5 stars.
    franchise: Arrival     # Titles sharing this link up in release order.
    duration: 116          # Minutes.
    release_date: 2016-11-11
    genres: [Drama, Sci-Fi]        # From the app's genre list, which differs for games.
    age_restriction: 13+           # all, 7+, 13+, 16+ or 18+.
    platforms: [Netflix, Prime Video]   # New names are added to the Platforms tab.
    watched_on: 2024-03-02
    description: A linguist is asked to talk to the aliens who have just landed.
    notes: Worth a rewatch.        # Your own thoughts.
    image: arrival.jpg             # A file in info/images/movie/.

# series.yaml
series:
  - name: The Wire
    year: 2002
    seasons: [13, 12, 12, 13, 10]   # Episodes per season.
    watched_through: S02E04         # Marks everything up to this episode watched.
    rating: 5
    episodes:                       # Optional details for any episode.
      S01E01:
        duration: 60
        rating: 4
        release_date: 2002-06-02
        description: The first episode.
  - name: Breaking Bad
    seasons: [7, 13, 13, 13, 16]
    status: completed               # Marks every episode watched.

# game.yaml
game:
  - name: Hades
    year: 2020
    singleplayer: true              # The default.
    status: playing                 # The singleplayer status. Or watching, which means the same.
    multiplayer: true               # Off by default.
    multiplayer_status: planned     # Like status, but it can't be completed.
    hours_played: 42
    ownership: owned                # Games only. wanted or owned, or left out for neither.
    platforms: [Steam]
    watched_on: 2024-08-17          # The day you finished it.
```

A game needs `singleplayer`, `multiplayer` or both, and a mode that's off can't have a status.

In the app, a series starts without seasons. Use **New season** on its page to add one, and the pencil icon next to a season to change its size or episode details. Only the last season can be deleted.

The home page's Upcoming section shows releases in the next 90 days, and Just released shows the last 30. A series appears through its own `release_date` or any episode's, so give a new season's first episode a date to see it there.

Titles are matched by kind, name (ignoring case) and year, so two films can share a name if their years differ.

If any entry is invalid, the import lists every problem and writes nothing. That includes an `image` file that isn't in the matching folder, such as `info/images/game/` for a game.

Images can be `.jpg`, `.jpeg`, `.png`, `.webp`, `.gif` or `.avif` files.

### Importing again

Importing is safe to repeat. Status, watch dates, progress and `ownership` are read only the first time a title is imported. After that the app owns them, so a new import can't undo anything you did in the web UI.

A repeat import does these things and nothing else:

- Adds new titles.
- Adds new platforms, fills in a platform's blank `url` or `release_year` and sets its `image`. A platform that already exists keeps its type, which you can change on its page.
- Adds episodes for longer `seasons` lists. A completed series goes back to watching.
- Fills in a rating, age restriction, duration, hours played, producer, franchise, release date, description, notes, genres or platforms that are still blank, for titles and episodes.
- Turns on `multiplayer`, with its `multiplayer_status`, for a game where neither the app nor an earlier import has set it. If that entry also says `singleplayer: false`, singleplayer goes off too, unless the game already has a status for it.
- Sets the `image`, replacing any previous one. Images can only be set from `info/`.

To rebuild everything from `info/`, delete `local/plasma.db` and run `make import`. This discards all progress recorded in the web UI.
