"""SQLite access: connections and schema migrations.

Migrations are numbered SQL files in `receipt_tracker/db/migrations/`, named
`NNNN_description.sql`. Each is applied once, in order, inside a transaction,
and recorded in the `schema_migrations` table.
"""

import re
import sqlite3
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the database, creating its folder if needed.

    Foreign keys are enforced and rows are addressable by column name. The
    connection is in autocommit mode; use `with conn:` or explicit BEGIN for
    multi-statement transactions.
    """
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_migrations() -> list[Migration]:
    """Read the packaged migration files, sorted by version."""
    found = []
    for entry in resources.files("receipt_tracker.db").joinpath("migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append(Migration(int(match[1]), match[2], entry.read_text(encoding="utf-8")))
    found.sort(key=lambda m: m.version)
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"Duplicate migration versions: {versions}")
    return found


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ) STRICT
        """
    )
    return {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}


def migrate(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> list[Migration]:
    """Apply every migration not yet recorded. Returns the ones applied.

    Each migration runs in its own transaction together with its bookkeeping
    row, so a failing migration leaves the database exactly as it was.
    """
    if migrations is None:
        migrations = load_migrations()
    done = applied_versions(conn)
    applied = []
    for migration in sorted(migrations, key=lambda m: m.version):
        if migration.version in done:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _split_statements(migration.sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(migration)
    return applied


def _split_statements(sql: str) -> list[str]:
    """Split a script into complete statements.

    `executescript` would be simpler but commits implicitly, which would break
    the all-or-nothing guarantee. `sqlite3.complete_statement` handles
    semicolons inside strings and comments correctly.
    """
    statements = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if _has_sql(buffer):
                statements.append(buffer.strip())
            buffer = ""
    if _has_sql(buffer):
        raise ValueError(f"Migration ends with an incomplete statement: {buffer.strip()[:80]!r}")
    return statements


def _has_sql(text: str) -> bool:
    """True if the text holds anything other than whitespace and `--` comments."""
    return any(line.strip() and not line.strip().startswith("--") for line in text.splitlines())
