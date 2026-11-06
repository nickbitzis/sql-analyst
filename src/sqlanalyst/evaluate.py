"""Execution accuracy: does the agent's final query return the same rows as a reference query?

Comparing results instead of SQL text is the standard way to grade
text-to-SQL, because there are many correct ways to write the same query.
The comparison is forgiving where it should be:

- Column names and column order do not matter.
- Extra columns are fine if some subset of them matches the reference.
- Row order does not matter. A "top 5" question still has to return the right five.
- Numbers are compared after rounding, so 12.3 and 12.30000001 match.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from itertools import permutations
from pathlib import Path

from .db import ReadOnlyDB

CANT_ANSWER = re.compile(r"\b(not (?:available|included|in|tracked|recorded|possible)|no (?:data|information|table|column)|doesn'?t (?:have|contain|include|track)|does not (?:have|contain|include|track)|cannot|can'?t)\b", re.I)


@dataclass
class Question:
    question: str
    sql: str | None  # None means the data cannot answer it

    @property
    def answerable(self) -> bool:
        return self.sql is not None


def load_questions(path: str | Path) -> list[Question]:
    out = []
    for n, line in enumerate(Path(path).read_text().splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if "question" not in row:
            raise ValueError(f"line {n}: missing 'question'")
        out.append(Question(row["question"], row.get("sql")))
    return out


def full_result(db: ReadOnlyDB, sql: str, limit: int = 10_000) -> list[tuple]:
    saved = db.max_rows
    db.max_rows = limit
    try:
        return db.query(sql).rows
    finally:
        db.max_rows = saved


def _norm(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    if isinstance(v, str):
        try:
            return round(float(v), 2)
        except ValueError:
            return v.strip().lower()
    return v


def results_match(reference: list[tuple], candidate: list[tuple]) -> bool:
    if len(reference) != len(candidate):
        return False
    if not reference:
        return True
    ref = Counter(tuple(_norm(v) for v in row) for row in reference)
    width, cwidth = len(reference[0]), len(candidate[0])
    if cwidth < width or cwidth > 8:
        return False
    cand = [tuple(_norm(v) for v in row) for row in candidate]
    for cols in permutations(range(cwidth), width):
        if Counter(tuple(row[i] for i in cols) for row in cand) == ref:
            return True
    return False


@dataclass
class Graded:
    question: Question
    passed: bool
    reason: str
    run: object = None


def grade(db: ReadOnlyDB, q: Question, run) -> Graded:
    if not q.answerable:
        ok = bool(CANT_ANSWER.search(run.answer or ""))
        return Graded(q, ok, "said the data cannot answer it" if ok else "answered a question the data cannot answer", run)
    if run.final_query is None:
        return Graded(q, False, f"no successful query (stop: {run.stop})", run)
    try:
        got = full_result(db, run.final_query)
    except Exception as exc:  # noqa: BLE001 - any failure here is a failed answer
        return Graded(q, False, f"final query failed on re-run: {exc}", run)
    ok = results_match(full_result(db, q.sql), got)
    return Graded(q, ok, "same rows as the reference" if ok else "different rows from the reference", run)


def check_references(db: ReadOnlyDB, questions: list[Question]) -> list[tuple[Question, str]]:
    """Run every reference query, so a broken eval set fails loudly before any model call."""
    out = []
    for q in questions:
        if q.sql is None:
            out.append((q, "(unanswerable)"))
        else:
            res = db.query(q.sql)
            out.append((q, res.as_text()))
    return out
