from conftest import FakeClient, response, text, tool

from sqlanalyst.agent import TOOLS, ask


def test_happy_path_runs_the_query_and_returns_the_answer(db):
    client = FakeClient(
        response(text("Let me count."), tool("run_query", sql="SELECT COUNT(*) FROM products")),
        response(text("There are 33 products.")),
    )
    run = ask("How many products?", db, client=client)
    assert run.answer == "There are 33 products."
    assert run.stop == "end_turn"
    assert run.queries == ["SELECT COUNT(*) FROM products"]
    assert run.final_query == "SELECT COUNT(*) FROM products"
    assert (run.input_tokens, run.output_tokens) == (200, 40)

    second = client.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    result = second[2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"
    assert "33" in result["content"] and "is_error" not in result


def test_request_shape(db):
    client = FakeClient(response(text("ok")))
    ask("q", db, client=client, model="claude-opus-5-5", effort="low")
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["output_config"] == {"effort": "low"}
    assert req["tools"] == TOOLS and all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in TOOLS)
    # The schema goes in a cached system block, since it is identical on every turn.
    (block,) = req["system"]
    assert block["cache_control"] == {"type": "ephemeral"}
    assert "CREATE TABLE orders" in block["text"]
    assert "thinking" not in req  # always on for this model; depth is set with effort


def test_errors_go_back_to_the_model_and_it_can_recover(db):
    client = FakeClient(
        response(tool("run_query", sql="SELECT revenue FROM orders")),
        response(tool("run_query", id="t2", sql="SELECT COUNT(*) FROM orders")),
        response(text("Done.")),
    )
    run = ask("q", db, client=client)
    assert [s.ok for s in run.steps] == [False, True]
    err = client.requests[1]["messages"][2]["content"][0]
    assert err["is_error"] is True and "no such column" in err["content"]
    assert run.final_query == "SELECT COUNT(*) FROM orders"


def test_blocked_write_is_an_error_result_not_a_crash(db):
    client = FakeClient(response(tool("run_query", sql="DROP TABLE orders")), response(text("I can't do that.")))
    run = ask("delete everything", db, client=client)
    assert run.steps[0].ok is False and "read-only" in run.steps[0].output
    assert run.final_query is None


def test_parallel_tool_calls_all_get_results(db):
    client = FakeClient(
        response(tool("sample_rows", id="a", table="orders"), tool("sample_rows", id="b", table="customers")),
        response(text("Seen both.")),
    )
    run = ask("q", db, client=client)
    results = client.requests[1]["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert len(run.steps) == 2 and run.final_query is None


def test_step_limit_stops_a_loop(db):
    looping = [response(tool("run_query", id=f"t{i}", sql="SELECT 1")) for i in range(10)]
    run = ask("q", db, client=FakeClient(*looping), max_steps=3)
    assert run.stop == "max_steps"
    assert len(run.steps) == 3
    assert "Stopped after 3" in run.answer


def test_refusal_is_reported(db):
    run = ask("q", db, client=FakeClient(response(text(""), stop="refusal")))
    assert run.stop == "refusal" and "declined" in run.answer


def test_unknown_tool_is_an_error(db):
    client = FakeClient(response(tool("drop_everything")), response(text("ok")))
    run = ask("q", db, client=client)
    assert run.steps[0].ok is False and "Unknown tool" in run.steps[0].output


def test_on_step_callback_sees_each_step(db):
    seen = []
    client = FakeClient(response(tool("run_query", sql="SELECT 1")), response(text("1")))
    ask("q", db, client=client, on_step=seen.append)
    assert [s.tool for s in seen] == ["run_query"]
