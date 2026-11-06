"""The agent: Claude plus three tools, run in an explicit loop.

The loop is written out by hand instead of using the SDK's tool runner so
each step is visible: every tool call is recorded in a trace, the number of
steps is capped, and errors go back to the model as tool results it can
correct, instead of ending the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .db import QueryError, ReadOnlyDB

SYSTEM = """You are a careful data analyst. Answer the user's question about the Trailhead store using the SQLite database you can query with the tools.

How to work:
- The database schema is below. Use sample_rows if you need to see what values look like.
- Write SQLite SQL. Dates are ISO text, so use strftime() or substr() for months and years.
- Revenue is SUM(order_items.quantity * order_items.unit_price) over orders with status 'completed', unless the question says otherwise.
- Prefer one query that computes the answer over several that return raw rows.
- If a query fails, read the error and fix the query.
- If the question cannot be answered from this data, say so and say what is missing.

When you are done, reply with a short answer in plain sentences, with the key numbers. Do not repeat the SQL in the answer; it is shown separately.

<schema>
{schema}
</schema>"""

TOOLS = [
    {
        "name": "run_query",
        "description": "Run one read-only SQLite SELECT statement and get the result as a text table. At most 50 rows come back, so aggregate in SQL instead of fetching raw rows. Writes are blocked.",
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "A single SELECT (or WITH ... SELECT) statement."}},
            "required": ["sql"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "sample_rows",
        "description": "Show the first few rows of a table, to see what its values look like before writing a query.",
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "Table name."}},
            "required": ["table"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


@dataclass
class Step:
    tool: str
    input: dict
    output: str
    ok: bool


@dataclass
class Run:
    question: str
    answer: str = ""
    steps: list[Step] = field(default_factory=list)
    stop: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def queries(self) -> list[str]:
        return [s.input["sql"] for s in self.steps if s.tool == "run_query"]

    @property
    def final_query(self) -> str | None:
        """The last query that succeeded. The eval treats it as the agent's answer."""
        ok = [s.input["sql"] for s in self.steps if s.tool == "run_query" and s.ok]
        return ok[-1] if ok else None


def call_tool(db: ReadOnlyDB, name: str, args: dict) -> tuple[str, bool]:
    try:
        if name == "run_query":
            return db.query(str(args.get("sql", ""))).as_text(), True
        if name == "sample_rows":
            return db.sample(str(args.get("table", ""))).as_text(), True
        return f"Unknown tool {name!r}.", False
    except QueryError as exc:
        return f"Error: {exc}", False


def ask(
    question: str,
    db: ReadOnlyDB,
    client=None,
    model: str = "claude-opus-5-5",
    effort: str = "medium",
    max_steps: int = 8,
    on_step=None,
) -> Run:
    """Run the agent until it answers, refuses, or reaches `max_steps` tool calls."""
    if client is None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise RuntimeError("Install the Claude extra first: uv sync --extra claude") from exc
        client = anthropic.Anthropic()

    run = Run(question)
    system = SYSTEM.format(schema=db.schema())
    messages: list[dict] = [{"role": "user", "content": question}]
    while True:
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            messages=messages,
            output_config={"effort": effort},
        )
        run.input_tokens += getattr(response.usage, "input_tokens", 0) or 0
        run.output_tokens += getattr(response.usage, "output_tokens", 0) or 0
        text = "".join(b.text for b in response.content if b.type == "text").strip()

        if response.stop_reason == "refusal":
            run.answer, run.stop = "The model declined to answer this question.", "refusal"
            return run
        calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not calls:
            run.answer, run.stop = text, response.stop_reason
            return run
        if len(run.steps) >= max_steps:
            run.answer = text or f"Stopped after {max_steps} tool calls without a final answer."
            run.stop = "max_steps"
            return run

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for call in calls:
            output, ok = call_tool(db, call.name, dict(call.input))
            step = Step(call.name, dict(call.input), output, ok)
            run.steps.append(step)
            if on_step:
                on_step(step)
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": output, **({} if ok else {"is_error": True})})
        messages.append({"role": "user", "content": results})
