# plasma

Personal TV series &amp; movies watch tracker. A FastAPI app with server-rendered pages, backed by a local SQLite file.

- Track movies and series as planned, watching, completed or dropped, with ratings and notes. Titles start with no status.
- Tick off episodes one at a time or by season. A series' status follows its progress.
- See what to watch next on the home page, and search and filter the library.
- Keep a duration, release date and description for every movie and episode.
- See what's upcoming and what just came out on the home page.
- Keep one central list of platforms, and record where each title can be watched.
- Mark favorites, and show an image on each title's page.
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

The app needs the `info/` folder, an `info/images/` folder and an `info/library.yaml` file. If any of them is missing, `make serve` and `make import` create it empty. `make serve` then stops with an error explaining why, so add your titles, run `make import` and start it again. An empty `library.yaml` is valid, so the next `make serve` starts normally.

Every `.yaml` or `.yml` file in `info/` is imported. Each one can have a `movies` list, a `series` list, or both:

```yaml
movies:
  - name: Arrival          # Required.
    year: 2016
    status: completed      # planned, watching, completed or dropped. Blank by default.
    rating: 9              # 1 to 10.
    duration: 116          # Minutes.
    release_date: 2016-11-11
    platforms: [Netflix, Prime Video]   # New names are added to the Platforms tab.
    watched_on: 2024-03-02
    description: A linguist is asked to talk to the aliens who have just landed.
    notes: Worth a rewatch.        # Your own thoughts.
    image: arrival.jpg             # A file in info/images/.

series:
  - name: The Wire
    year: 2002
    seasons: [13, 12, 12, 13, 10]   # Episodes per season.
    watched_through: S02E04         # Marks everything up to this episode watched.
    rating: 10
    episodes:                       # Optional details for any episode.
      S01E01:
        duration: 60
        release_date: 2002-06-02
        description: The first episode.
  - name: Breaking Bad
    seasons: [7, 13, 13, 13, 16]
    status: completed               # Marks every episode watched.
```

The home page's Upcoming section shows releases in the next 90 days, and Just released shows the last 30. A series appears through its own `release_date` or any episode's, so give a new season's first episode a date to see it there.

Titles are matched by kind, name (ignoring case) and year, so two films can share a name if their years differ.

If any entry is invalid, the import lists every problem and writes nothing. That includes an `image` file that isn't in `info/images/`.

Images can be `.jpg`, `.jpeg`, `.png`, `.webp`, `.gif` or `.avif` files.

### Importing again

Importing is safe to repeat. Status, watch dates and progress are read only the first time a title is imported. After that the app owns them, so a new import can't undo anything you did in the web UI.

A repeat import does these four things and nothing else:

- Adds new titles.
- Adds episodes for longer `seasons` lists. A completed series goes back to watching.
- Fills in a rating, duration, release date, description, notes or platforms that are still blank, for titles and episodes.
- Sets the `image`, replacing any previous one. Images can only be set from `info/`.

To rebuild everything from `info/`, delete `local/plasma.db` and run `make import`. This discards all progress recorded in the web UI.
