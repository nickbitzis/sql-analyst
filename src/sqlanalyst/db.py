"""Read-only SQL access with several independent guardrails.

A model writes the SQL here, so the database has to be safe against any
statement, not just the ones we expect. Each layer below would stop a write
on its own:

1. The file is opened with SQLite's read-only URI mode.
2. An authorizer callback allows only reads, so writes, ATTACH, and PRAGMA
   are refused while the statement is being compiled.
3. Only one statement runs per call.
4. A progress handler cancels queries that run too long, such as an
   accidental cross join.
5. Results are capped at a fixed number of rows, so a careless SELECT * does
   not flood the model's context.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

ALLOWED = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),  # WITH RECURSIVE
}


class QueryError(Exception):
    """A query the model can learn from: bad SQL, a blocked statement, or a timeout."""


@dataclass
class Result:
    columns: list[str]
    rows: list[tuple]
    truncated: bool = False
    notes: list[str] = field(default_factory=list)

    def as_text(self, max_width: int = 40) -> str:
        """A compact plain-text table, which models read reliably."""
        if not self.columns:
            return "(no columns)"
        cells = [[_fmt(v, max_width) for v in row] for row in self.rows]
        widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(self.columns)]
        line = lambda vals: " | ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()
        out = [line(self.columns), "-+-".join("-" * w for w in widths)]
        out += [line(r) for r in cells]
        n = len(self.rows)
        out.append(f"({n} row{'s' if n != 1 else ''}{', truncated' if self.truncated else ''})")
        return "\n".join(out + self.notes)


def _fmt(v, max_width: int) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        s = f"{v:.4f}".rstrip("0").rstrip(".") if abs(v) < 1e15 else str(v)
    else:
        s = str(v)
    return s if len(s) <= max_width else s[: max_width - 3] + "..."


class ReadOnlyDB:
    def __init__(self, path: str | Path, max_rows: int = 50, max_steps: int = 20_000_000):
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Database not found: {path}. Run `sqlanalyst init-db` first.")
        self.path = path
        self.max_rows = max_rows
        self.max_steps = max_steps
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.con.set_authorizer(self._authorize)

    @staticmethod
    def _authorize(action, arg1, arg2, dbname, source):
        if action in ALLOWED:
            return sqlite3.SQLITE_OK
        # PRAGMA table_info and friends are read-only and useful for schema discovery.
        if action == sqlite3.SQLITE_PRAGMA and arg1 in {"table_info", "foreign_key_list", "index_list"}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def query(self, sql: str) -> Result:
        sql = sql.strip().rstrip(";").strip()
        if not sql:
            raise QueryError("Empty query.")
        if not sqlite3.complete_statement(sql + ";") or _has_second_statement(sql):
            raise QueryError("Send exactly one SQL statement.")
        steps = 0

        def progress():
            nonlocal steps
            steps += 1
            return 1 if steps * 1000 > self.max_steps else 0

        self.con.set_progress_handler(progress, 1000)
        try:
            cur = self.con.execute(sql)
            columns = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(self.max_rows + 1)
        except sqlite3.DatabaseError as exc:
            msg = str(exc)
            if "interrupted" in msg:
                raise QueryError("Query cancelled: it ran too long. Add filters or aggregate first.") from exc
            if "not authorized" in msg:
                raise QueryError("Blocked: this database is read-only and allows SELECT statements only.") from exc
            raise QueryError(msg) from exc
        finally:
            self.con.set_progress_handler(None, 0)
        truncated = len(rows) > self.max_rows
        notes = [f"Only the first {self.max_rows} rows are shown. Aggregate or add LIMIT to see what matters."] if truncated else []
        return Result(columns, [tuple(r) for r in rows[: self.max_rows]], truncated, notes)

    def tables(self) -> list[str]:
        rows = self.con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        return [r[0] for r in rows]

    def schema(self) -> str:
        """CREATE statements with their comments, plus row counts. This is what the model sees first."""
        parts = []
        for name, sql in self.con.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"):
            (count,) = self.con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()
            parts.append(f"{sql.strip()};\n-- {count:,} rows")
        return "\n\n".join(parts)

    def sample(self, table: str, n: int = 3) -> Result:
        if table not in self.tables():
            raise QueryError(f"No table named {table!r}. Tables: {', '.join(self.tables())}.")
        return self.query(f'SELECT * FROM "{table}" LIMIT {int(n)}')

    def close(self):
        self.con.close()


def _has_second_statement(sql: str) -> bool:
    """True if anything other than whitespace or comments follows the first statement."""
    in_str = None
    for i, ch in enumerate(sql):
        if in_str:
            if ch == in_str:
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
        elif ch == ";":
            rest = sql[i + 1 :].strip()
            return bool(rest) and not all(line.strip().startswith("--") or not line.strip() for line in rest.splitlines())
    return False
