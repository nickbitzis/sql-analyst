from pathlib import Path

import pytest
from conftest import FakeClient, response, text, tool

from sqlanalyst import cli
from sqlanalyst.agent import Run, Step, ask
from sqlanalyst.evaluate import (
    Question,
    check_references,
    grade,
    load_questions,
    results_match,
)

QUESTIONS = Path(__file__).resolve().parents[1] / "examples" / "questions.jsonl"


def test_match_ignores_names_order_and_float_noise():
    assert results_match([("West", 10.0)], [(10.000001, "west")])
    assert results_match([("a", 1), ("b", 2)], [("b", 2), ("a", 1)])


def test_match_allows_extra_columns_but_not_extra_rows():
    assert results_match([("Mountain",)], [("Mountain", 1.61, 348026)])
    assert not results_match([("Mountain",)], [("Mountain",), ("West",)])
    assert not results_match([("a", 1)], [("a",)])
    assert not results_match([("a", 1)], [("a", 2)])


def test_match_handles_numbers_returned_as_text():
    assert results_match([(22.8,)], [("22.80",)])


def test_every_reference_query_runs(db):
    qs = load_questions(QUESTIONS)
    assert len(qs) == 14 and sum(not q.answerable for q in qs) == 2
    for q, out in check_references(db, qs):
        assert "row" in out or out == "(unanswerable)"


def run_with(*sqls, answer="ok"):
    r = Run("q", answer=answer, stop="end_turn")
    r.steps = [Step("run_query", {"sql": s}, "", ok) for s, ok in sqls]
    return r


def test_grade_uses_the_last_successful_query(db):
    q = Question("How many products?", "SELECT COUNT(*) FROM products")
    good = run_with(("SELECT COUNT(*) FROM orders", True), ("SELECT COUNT(*) AS n FROM products", True), ("SELECT nope", False))
    assert grade(db, q, good).passed
    wrong = run_with(("SELECT COUNT(*) FROM orders", True))
    assert not grade(db, q, wrong).passed
    none = run_with(("SELECT nope", False))
    assert grade(db, q, none).reason.startswith("no successful query")


@pytest.mark.parametrize(
    "answer, passed",
    [
        ("The database does not contain marketing spend, so I can't answer that.", True),
        ("There is no data on satisfaction scores.", True),
        ("Marketing spend was $120,000.", False),
    ],
)
def test_unanswerable_questions_pass_only_when_the_agent_says_so(db, answer, passed):
    q = Question("What was marketing spend?", None)
    assert grade(db, q, run_with(answer=answer)).passed is passed


def test_end_to_end_with_a_scripted_agent(db):
    """A fake model that writes the reference SQL scores 100%."""
    qs = [q for q in load_questions(QUESTIONS) if q.answerable][:3]
    for q in qs:
        client = FakeClient(response(tool("run_query", sql=q.sql)), response(text("answer")))
        assert grade(db, q, ask(q.question, db, client=client)).passed


def test_cli_sql_and_schema(db_path, capsys):
    assert cli.main(["--db", str(db_path), "sql", "SELECT COUNT(*) AS n FROM products"]) == 0
    assert "33" in capsys.readouterr().out
    assert cli.main(["--db", str(db_path), "sql", "DELETE FROM products"]) == 1
    assert "read-only" in capsys.readouterr().err
    assert cli.main(["--db", str(db_path), "schema"]) == 0
    assert "CREATE TABLE customers" in capsys.readouterr().out


def test_cli_eval_check_needs_no_model(db_path, capsys):
    assert cli.main(["--db", str(db_path), "eval", str(QUESTIONS), "--check"]) == 0
    assert "All 14 reference queries ran." in capsys.readouterr().out


def test_cli_eval_scores_runs(db_path, capsys, monkeypatch):
    def fake_ask(question, db, **kw):
        q = next(q for q in load_questions(QUESTIONS) if q.question == question)
        return run_with((q.sql, True)) if q.sql else run_with(answer="That data is not available.")

    monkeypatch.setattr(cli, "ask", fake_ask)
    assert cli.main(["--db", str(db_path), "eval", str(QUESTIONS)]) == 0
    assert "Execution accuracy: 14/14 (100%)" in capsys.readouterr().out


def test_cli_ask_prints_steps_and_summary(db_path, capsys, monkeypatch):
    def fake_ask(question, db, on_step=None, **kw):
        r = run_with(("SELECT 1", True), answer="One.")
        r.input_tokens, r.output_tokens = 1500, 80
        if on_step:
            on_step(Step("run_query", {"sql": "SELECT 1"}, "1\n-\n1\n(1 row)", True))
        return r

    monkeypatch.setattr(cli, "ask", fake_ask)
    assert cli.main(["--db", str(db_path), "ask", "q"]) == 0
    out = capsys.readouterr().out
    assert "[query]" in out and "  SELECT 1" in out and "One." in out and "1,500 input" in out


def test_cli_missing_custom_db_exits_2(tmp_path, capsys):
    assert cli.main(["--db", str(tmp_path / "x.db"), "schema"]) == 2
