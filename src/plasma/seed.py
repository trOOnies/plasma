"""Fill the local library with the titles in demo.yaml."""

import sys
from pathlib import Path

from plasma.db import connect
from plasma.importer import import_paths

DEMO_FILE = Path(__file__).parent / "demo.yaml"


def main() -> int:
    conn = connect()
    try:
        result = import_paths(conn, [DEMO_FILE])
    finally:
        conn.close()
    print(f"Demo data: {result.added} added, {result.unchanged + result.updated} already present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
