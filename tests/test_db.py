import sqlite3

import pytest

from sqlanalyst.data import build
from sqlanalyst.db import QueryError, ReadOnlyDB, Result


def test_select_works_and_formats_as_a_table(db):
    res = db.query("SELECT category, COUNT(*) AS n FROM products GROUP BY category ORDER BY category LIMIT 2")
    assert res.columns == ["category", "n"]
    assert res.rows == [("Apparel", 6), ("Cooking", 5)]
    assert res.as_text().splitlines() == ["category | n", "---------+--", "Apparel  | 6", "Cooking  | 5", "(2 rows)"]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "UPDATE products SET unit_price = 0",
        "INSERT INTO customers VALUES (9999, 'x', 'West', 'consumer', '2025-01-01')",
        "DROP TABLE orders",
        "CREATE TABLE t (x)",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA writable_schema = ON",
        "PRAGMA journal_mode = DELETE",
    ],
)
def test_writes_and_escapes_are_blocked(db, sql):
    with pytest.raises(QueryError):
        db.query(sql)
    assert db.query("SELECT COUNT(*) FROM orders").rows[0][0] > 0


def test_blocked_message_tells_the_model_why(db):
    with pytest.raises(QueryError, match="read-only"):
        db.query("DELETE FROM orders")


def test_file_is_opened_read_only_even_without_the_authorizer(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        con.execute("DELETE FROM orders")


def test_only_one_statement_per_call(db):
    with pytest.raises(QueryError, match="one SQL statement"):
        db.query("SELECT 1; DELETE FROM orders")
    # A trailing semicolon, a comment, or a semicolon inside a string is fine.
    assert db.query("SELECT 1;").rows == [(1,)]
    assert db.query("SELECT 1; -- done").rows == [(1,)]
    assert db.query("SELECT 'a;b'").rows == [("a;b",)]


def test_read_only_pragmas_are_allowed(db):
    cols = [r[1] for r in db.query("PRAGMA table_info(orders)").rows]
    assert cols == ["id", "customer_id", "order_date", "status", "channel"]


def test_runaway_queries_are_cancelled(db_path):
    small = ReadOnlyDB(db_path, max_steps=200_000)
    with pytest.raises(QueryError, match="ran too long"):
        small.query("SELECT COUNT(*) FROM orders a, orders b")
    # The connection still works afterwards.
    assert small.query("SELECT 1").rows == [(1,)]


def test_results_are_capped(db_path):
    capped = ReadOnlyDB(db_path, max_rows=10)
    res = capped.query("SELECT id FROM orders")
    assert len(res.rows) == 10 and res.truncated
    assert "truncated" in res.as_text() and "Aggregate" in res.as_text()


def test_bad_sql_returns_the_sqlite_error(db):
    with pytest.raises(QueryError, match="no such column"):
        db.query("SELECT revenue FROM orders")


def test_sample_rejects_unknown_tables(db):
    assert len(db.sample("products").rows) == 3
    with pytest.raises(QueryError, match="Tables: customers"):
        db.sample("users")


def test_schema_includes_comments_and_counts(db):
    s = db.schema()
    assert "-- completed, returned, or cancelled" in s
    assert "-- 33 rows" in s


def test_missing_database_has_a_helpful_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="init-db"):
        ReadOnlyDB(tmp_path / "nope.db")


def test_formatting_of_nulls_floats_and_long_text():
    r = Result(["a", "b", "c"], [(None, 2.50, "x" * 60)])
    row = r.as_text().splitlines()[2]
    assert row.startswith("NULL | 2.5 | xxxx") and row.endswith("...")


def test_data_is_deterministic_and_has_the_planted_patterns(tmp_path, db):
    other = ReadOnlyDB(build(tmp_path / "again.db"))
    q = "SELECT COUNT(*), SUM(quantity * unit_price) FROM order_items"
    assert other.query(q).rows == db.query(q).rows
    rates = db.query(
        "SELECT p.category, AVG(o.status = 'returned') FROM orders o JOIN order_items i ON i.order_id = o.id "
        "JOIN products p ON p.id = i.product_id GROUP BY 1 ORDER BY 2 DESC"
    ).rows
    assert rates[0][0] == "Footwear" and rates[0][1] > 2 * rates[1][1]
    months = db.query("SELECT substr(order_date, 6, 2), COUNT(*) FROM orders GROUP BY 1 ORDER BY 2 DESC LIMIT 1").rows
    assert months[0][0] == "12"
