# plasma

Personal TV series &amp; movies watch tracker. A FastAPI app with server-rendered pages, backed by a local SQLite file.

- Track movies and series as planned, watching, completed or dropped, with ratings and notes.
- Tick off episodes one at a time or by season. A series' status follows its progress.
- See what to watch next on the home page, and search and filter the library.
- Load your titles from YAML files in `info/`.

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

The app has no login, so keep it bound to `127.0.0.1` (the default).

## Your data

Both of these folders are gitignored:

- `info/` holds your input files.
- `local/plasma.db` is the database. Set `PLASMA_DB` to keep it somewhere else.

### Files in `info/`

Every `.yaml` or `.yml` file in `info/` is imported. Each one can have a `movies` list, a `series` list, or both:

```yaml
movies:
  - name: Arrival          # Required.
    year: 2016
    status: completed      # planned (default), watching, completed or dropped.
    rating: 9              # 1 to 10.
    watched_on: 2024-03-02
    notes: Worth a rewatch.

series:
  - name: The Wire
    year: 2002
    seasons: [13, 12, 12, 13, 10]   # Episodes per season.
    watched_through: S02E04         # Marks everything up to this episode watched.
    rating: 10
  - name: Breaking Bad
    seasons: [7, 13, 13, 13, 16]
    status: completed               # Marks every episode watched.
```

Titles are matched by kind, name (ignoring case) and year, so two films can share a name if their years differ.

If any entry is invalid, the import lists every problem and writes nothing.

### Importing again

Importing is safe to repeat. Status, watch dates and progress are read only the first time a title is imported. After that the app owns them, so a new import can't undo anything you did in the web UI.

A repeat import does these three things and nothing else:

- Adds new titles.
- Adds episodes for longer `seasons` lists. A completed series goes back to watching.
- Fills in a rating or notes that are still blank.

To rebuild everything from `info/`, delete `local/plasma.db` and run `make import`. This discards all progress recorded in the web UI.
