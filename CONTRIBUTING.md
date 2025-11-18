# Development notes

Run the tests with coverage:

```sh
uv run coverage run --data-file=.coverage.mcp tests/mcp_test.py
uv run coverage run --data-file=.coverage.jsonrpc tests/jsonrpc_test.py
```

Combine coverage and generate report:

```sh
uv run coverage combine
uv run coverage report
uv run coverage html
```

Generate report for just `jsonrpc_test.py:

```sh
uv run coverage html --data-file=.coverage.jsonrpc
```
