# sqlanalyst

Ask a business question in plain English and get an answer computed from a real database. Claude writes and runs SQL through tools, fixes its own errors, and answers with the numbers. The database is guarded so that no statement the model writes can change it, and an evaluation set measures how often the answers are right.

## Why

"Talk to your data" is one of the most requested AI features, and one of the easiest to get wrong. The hard parts are not the prompt:

- **Safety.** The model writes code that runs against your data. It has to be impossible for that code to modify anything, not just unlikely.
- **Cost and context.** One careless `SELECT *` can return a million rows. Results need a hard cap and a nudge to aggregate.
- **Recovery.** The first query is often wrong. Errors should go back to the model so it can fix them, instead of ending the run.
- **Measurement.** Without an eval set, nobody knows whether a prompt change made answers better or worse.

This project handles each of these in a small amount of readable code.

## How it works

```
question -> Claude -> run_query / sample_rows -> read-only SQLite -> result table -> Claude -> ... -> answer
```

- The agent loop in `agent.py` is written out by hand, not hidden in a framework. Each tool call is recorded in a trace, the number of calls is capped, and failed queries go back as `is_error` tool results.
- The schema, including column comments, goes in the system prompt and is marked for prompt caching, because it is identical on every turn of the loop.
- Both tools use strict input schemas, so the model's tool inputs always match the declared shape.
- Requests use Claude Opus 5.5 at medium effort by default. `--effort` changes it.

## Guardrails

Any one of these would stop a write on its own. They are layered because the SQL comes from a model.

| Layer | What it does |
|---|---|
| Read-only file mode | The database is opened with SQLite's `mode=ro`, so the engine itself refuses writes. |
| Authorizer callback | Every statement is checked while it compiles. Only reads are allowed, so `DELETE`, `DROP`, `ATTACH`, and `PRAGMA` writes fail before they run. |
| One statement per call | `SELECT 1; DELETE ...` is rejected, while semicolons inside strings and trailing comments are fine. |
| Time limit | A progress handler cancels queries that run too long, such as an accidental cross join. |
| Row cap | At most 50 rows come back, with a note telling the model to aggregate. |

```
$ uv run sqlanalyst sql "DROP TABLE orders"
error: Blocked: this database is read-only and allows SELECT statements only.

$ uv run sqlanalyst sql "SELECT 1; DELETE FROM orders"
error: Send exactly one SQL statement.
```

The `sql` command runs a query through the same guardrails the agent uses, which is handy for checking the data by hand.

## The sample data

`data.py` builds Trailhead, a made-up outdoor gear store: 1,200 customers, 33 products, and about 8,000 orders over 2024 and 2025. It is generated from a fixed seed, so every copy is identical and eval answers never drift. A few patterns are planted so questions have something to find: a holiday peak, a region that grows much faster than the others, and a category that gets returned far more often.

```
$ uv run sqlanalyst sql "SELECT p.category, ROUND(100.0 * AVG(o.status = 'returned'), 1) AS return_pct
    FROM orders o JOIN order_items i ON i.order_id = o.id JOIN products p ON p.id = i.product_id
    GROUP BY 1 ORDER BY 2 DESC LIMIT 3"
category   | return_pct
-----------+-----------
Footwear   | 18.8
Cooking    | 7.5
Navigation | 7.1
(3 rows)
```

The database is built automatically on first use, or with `sqlanalyst init-db`.

## Ask questions

```bash
uv sync --extra claude
export ANTHROPIC_API_KEY=...   # or use a saved `ant auth login` profile
uv run sqlanalyst ask "Which region grew revenue the most from 2024 to 2025?"
```

The CLI prints each query as it runs, a preview of each result, then the answer and the token count. `-q` prints only the answer. Point `--db` at your own SQLite file to use it on other data.

## Evaluation

`examples/questions.jsonl` has 14 questions. Twelve come with a reference query. Two ask for data the store does not have, like marketing spend, and the right answer is to say so.

Grading uses **execution accuracy**, the standard metric for text-to-SQL: the agent's last successful query passes if it returns the same rows as the reference. Column names, column order, row order, and float noise are ignored, and extra columns are allowed. Comparing results instead of SQL text matters, because there are many correct ways to write the same query.

```bash
uv run sqlanalyst eval examples/questions.jsonl --check   # runs the reference queries only, no API calls
uv run sqlanalyst eval examples/questions.jsonl           # runs the agent on every question
```

The full eval makes one agent run per question, usually two to four model calls each.

## Tests

```bash
uv run --group dev pytest
```

44 tests, none of which call the API. The guardrail tests try every kind of write, escape, and runaway query. The agent tests drive the loop with a scripted fake client to check the request shape, error recovery, parallel tool calls, the step limit, and refusals. The grading tests check the result matcher and run the CLI end to end.

## License

MIT. See [LICENSE](LICENSE).
