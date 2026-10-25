"""Verify the shipped library with stdlib SQLite from an isolated temporary directory."""

from __future__ import annotations

import sqlite3
import struct
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path


def main() -> None:
    (wheel,) = Path("dist").glob("*.whl")
    assert "none-any" not in wheel.name, wheel.name
    with tempfile.TemporaryDirectory(prefix="yuki-vec1-wheel-") as temporary:
        with zipfile.ZipFile(wheel) as archive:
            files = [name for name in archive.namelist() if name.startswith("yuki/_native/")]
            (library,) = [name for name in files if name.endswith((".dll", ".so", ".dylib"))]
            # Extract one known wheel resource, not arbitrary paths from the archive.
            path = Path(temporary) / Path(library).name
            path.write_bytes(archive.read(library))
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.enable_load_extension(True)
            connection.load_extension(str(path))
            connection.enable_load_extension(False)
            connection.execute("CREATE VIRTUAL TABLE vectors USING vec1(vector, fact_id)")
            connection.executemany(
                "INSERT INTO vectors(rowid,vector,fact_id) VALUES(?,?,?)",
                (
                    (index, struct.pack("<2f", *values), index)
                    for index, values in ((1, (1, 0)), (2, (0, 1)))
                ),
            )
            result = connection.execute(
                "SELECT rowid FROM vectors(?, ?) WHERE fact_id IN (SELECT 2)",
                (struct.pack("<2f", 1, 0), '{"K":1}'),
            ).fetchall()
            assert result == [(2,)], result
            print(wheel.name, connection.execute("SELECT vec1_info()").fetchone()[0])


if __name__ == "__main__":
    main()
