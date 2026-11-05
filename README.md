<p align="center"><img src="src/plasma/static/plasma.svg" alt="" width="128" height="128"></p>

# plasma

Personal tracker for TV series, movies, games and the influencers you follow. A FastAPI app with server-rendered pages, backed by a local SQLite file.

- Track movies and series as planned, watching, completed or dropped, with notes and a rating of 1 to 5 stars, which episodes can have too. Titles start with no status.
- Link series, movies or games into a franchise, and step through it in release order. Each franchise holds one type only, so a Harry Potter movie franchise and game franchise stay separate.
- Track games too, with the hours you've played and whether you want or own each one. Singleplayer and multiplayer each keep their own status, and multiplayer can't be completed.
- Tag titles with genres and an age restriction, then filter the library by either.
- Group series, movies and games into plans, like playlists. Built-in plans hold everything you've marked with a heart (Favorites) or as planned (Planned).
- Add seasons with New season, change them with the pencil icon, and tick off episodes one at a time or by season. A series' status follows its progress.
- See what to watch next on the home page, and search, filter and page through the library.
- Keep a duration, release date and description for every movie and episode.
- See what's upcoming and what just came out on the home page.
- Keep one central list of platforms, split into Streaming Platforms and Game Consoles, and record where each title can be watched or played.
- Browse the Series, Movies and Games tabs as a grid of images. Mark favorites, which go to the top of their tab, and show an image on each title's and platform's page.
- Load your titles from YAML files in `info/`.
- Export your watch statuses and ratings to a small file, and import such a file on any machine.
- Compare with someone else's file on the Duo tab, to see what you could watch or play together.
- Follow content creators on the Influencers tab, with a profile picture, website and social media profiles, and make collages of notes, links, YouTube and TikTok videos and images about each one.

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

### Watch data files

The Export and Import cards, side by side at the bottom of the Duo tab, export a small JSON file with your watch statuses and ratings, and import one back in. The file holds every title with a status, rating, watch date, episode progress, favorite or ownership, and nothing else about it beyond what identifies it:

```json
{
  "plasma_watch_data": 2,
  "exported_on": "2026-11-02",
  "titles": [
    {"kind": "movie", "name": "Arrival", "year": 2016, "status": "completed", "rating": 5, "watched_on": "2024-03-02", "favorite": true},
    {"kind": "series", "name": "The Wire", "year": 2002, "status": "watching", "seasons": [13, 12], "watched": [[1, 1, 13], [2, 1, 4]], "episodes": {"S01E01": {"rating": 4}}},
    {"kind": "game", "name": "Hades", "year": 2020, "multiplayer": true, "multiplayer_status": "planned", "ownership": "owned"}
  ]
}
```

Each item of `watched` is a season and its first and last watched episodes. Anything blank is left out.

Loading a file overwrites what this machine has:

- Titles in the file take its status, multiplayer status, rating, watch date, favorite and ownership, even where the file leaves them blank. Version 1 files don't hold favorites or ownership, so loading one leaves those alone. So do their episodes' watched state and ratings. A title in the file that isn't in your library is added, even one you deleted.
- Titles that aren't in the file stay as they are, and so do seasons the file doesn't list. Everything else about a title, such as its notes, stays too.
- Episode watch dates aren't in the file. An episode that stays watched keeps its date, and a newly watched one has none.
- If anything in the file is invalid, the app lists every problem and changes nothing.
- A copy of the database is saved first, next to it, as `backup-before-load-<date and time>.db`. To undo a load, stop the app and put that copy in place of `plasma.db`.

Titles are matched the same way as in `info/`, and what a load changes survives `make import`.

### Duo

The Duo tab takes another person's watch data file and lists what you could enjoy together. It only reads the file, so your library doesn't change. The file is kept in the database until you replace or forget it.

| Section | What's in it |
| --- | --- |
| Favorites of you both | Titles you've both marked as favorites |
| Planned by you both | Titles you've both marked Planned |
| In progress for you both | Series and games you're both on, with where each of you is up to |
| Rewatch together | Series and movies you've both completed, unless one of you rated them under 4 stars |
| They loved, you haven't seen | Series, movies and multiplayer games they rated 4 or 5 stars and you haven't started |
| You loved, they haven't seen | The same the other way round |
| Multiplayer games you both own | Owned by both, with multiplayer on for either of you |
| Other games you both own | The rest of the games you both own |
| Games you both want | Marked Wanted by both |

Empty sections are hidden. Dropped counts as no status here, so it never rules a title in or out. Titles match by kind, name (ignoring case) and year, and a year missing on one side still matches. A title only they have can appear in They loved, without a link.

### Influencers

Influencers and their collages are only added in the app, never from `info/`, so `make import` leaves them alone and can't bring back anything you deleted. Profile pictures and collage images are stored in the database itself, so a copy of `local/plasma.db` holds them too. Watch data files don't include influencers.

A link in a collage must be an `http://` or `https://` address with a real site name, and `https://` is added when it's left out. Links that are valid but look risky are saved with a warning: not secure, a bare IP address, an unusual port, a name before an `@`, letters from other alphabets, a link shortener, a program download, a very long address, a duplicate in the same collage, or a video that could be a Video item. Videos must be a single YouTube video or Short, or a full TikTok video address.

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
    franchise: Arrival     # Movies sharing this link up in release order.
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

Importing is safe to repeat. Status, watch dates, progress and `ownership` are read only the first time a title is imported. After that the app owns them, so a new import can't undo anything you did in the web UI. Every button saves straight away, so nothing depends on the exit button.

That covers taking things away too. A field you emptied stays empty, and a title, platform, season or episode you deleted or renamed isn't added back, even though `info/` still lists it. To get a deleted title or platform back, add it again in the app.

A repeat import does these things and nothing else:

- Adds new titles, except ones you deleted or renamed in the app.
- Adds new platforms, except ones you deleted or renamed in the app, fills in a platform's blank `url` or `release_year` and sets its `image`. A platform that already exists keeps its type, which you can change on its page.
- Adds episodes for longer `seasons` lists, unless you removed them in the app and the list doesn't go past them. A completed series goes back to watching.
- Fills in a rating, age restriction, duration, hours played, producer, franchise, release date, description, notes, genres or platforms that are still blank, for titles and episodes, unless you emptied them in the app.
- Turns on `multiplayer`, with its `multiplayer_status`, for a game where neither the app nor an earlier import has set it. If that entry also says `singleplayer: false`, singleplayer goes off too, unless the game already has a status for it.
- Sets the `image`, replacing any previous one. Images can only be set from `info/`.

To rebuild everything from `info/`, delete `local/plasma.db` and run `make import`. This discards all progress recorded in the web UI, and brings back everything you removed there.
