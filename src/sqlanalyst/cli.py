"""Command line entry point: `sqlanalyst ask | sql | schema | eval | init-db`."""

from __future__ import annotations

import argparse
import sys
import textwrap
import time
from pathlib import Path

from . import __version__
from .agent import ask
from .data import build
from .db import QueryError, ReadOnlyDB
from .evaluate import check_references, grade, load_questions

HERE = Path(__file__).resolve().parents[2]
DEFAULT_DB = HERE / "data" / "trailhead.db"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sqlanalyst", description="Answer business questions with read-only SQL and Claude.")
    p.add_argument("--version", action="version", version=f"sqlanalyst {__version__}")
    p.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file (default: the bundled sample store)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="build the sample database from its fixed seed")
    sub.add_parser("schema", help="print the schema the model sees")

    sql = sub.add_parser("sql", help="run one query through the same guardrails the agent uses")
    sql.add_argument("query")

    a = sub.add_parser("ask", help="ask a question in plain English")
    a.add_argument("question")
    a.add_argument("--model", default="claude-opus-5-5")
    a.add_argument("--effort", default="medium", choices=("low", "medium", "high", "xhigh", "max"))
    a.add_argument("--max-steps", type=int, default=8)
    a.add_argument("-q", "--quiet", action="store_true", help="print only the answer")

    ev = sub.add_parser("eval", help="measure execution accuracy on labeled questions")
    ev.add_argument("questions")
    ev.add_argument("--check", action="store_true", help="only run the reference queries, no model calls")
    ev.add_argument("--model", default="claude-opus-5-5")
    ev.add_argument("--effort", default="medium", choices=("low", "medium", "high", "xhigh", "max"))
    ev.add_argument("--limit", type=int, help="only the first N questions")
    return p


def print_step(step) -> None:
    label = "query" if step.tool == "run_query" else f"sample {step.input.get('table')}"
    body = step.input.get("sql", "") if step.tool == "run_query" else ""
    print(f"\n[{label}]{'' if step.ok else ' failed'}")
    if body:
        print(textwrap.indent(body.strip(), "  "))
    lines = step.output.splitlines()
    shown = lines[:8] + ([f"... {len(lines) - 8} more lines"] if len(lines) > 8 else [])
    print(textwrap.indent("\n".join(shown), "  > "))


def ensure_db(path: Path) -> None:
    if not path.exists() and path == DEFAULT_DB:
        build(path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.db)
    try:
        if args.command == "init-db":
            build(path)
            print(f"Built {path}")
            return 0
        ensure_db(path)
        db = ReadOnlyDB(path)

        if args.command == "schema":
            print(db.schema())
            return 0

        if args.command == "sql":
            try:
                print(db.query(args.query).as_text())
            except QueryError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            return 0

        if args.command == "ask":
            start = time.monotonic()
            run = ask(args.question, db, model=args.model, effort=args.effort, max_steps=args.max_steps, on_step=None if args.quiet else print_step)
            if not args.quiet:
                print()
            print(textwrap.fill(run.answer, width=88) if "\n" not in run.answer else run.answer)
            if not args.quiet:
                print(f"\n{len(run.steps)} tool calls, {run.input_tokens:,} input and {run.output_tokens:,} output tokens, {time.monotonic() - start:.1f}s")
            return 0

        questions = load_questions(args.questions)[: args.limit]
        if args.check:
            for q, out in check_references(db, questions):
                print(f"## {q.question}\n{out}\n")
            print(f"All {len(questions)} reference queries ran.")
            return 0
        passed = 0
        for n, q in enumerate(questions, start=1):
            run = ask(q.question, db, model=args.model, effort=args.effort)
            g = grade(db, q, run)
            passed += g.passed
            print(f"{'PASS' if g.passed else 'FAIL'}  {n:>2}. {q.question}  ({g.reason}, {len(run.steps)} calls)")
        print(f"\nExecution accuracy: {passed}/{len(questions)} ({passed / len(questions):.0%})")
        return 0 if passed == len(questions) else 1
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
