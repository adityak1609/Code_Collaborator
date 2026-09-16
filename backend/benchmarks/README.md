# Concord benchmarks

Run these against a healthy Compose stack from `backend/`:

```powershell
python -m benchmarks.bench_websocket
python -m benchmarks.bench_execution
python -m benchmarks.bench_snapshots
```

Each command accepts `--api-url`. The WebSocket suite also accepts
`--server-pid` (or `BENCHMARK_SERVER_PID`). By default it reads memory from the
Compose container `code_collaborator-backend-1`; override that with
`--server-container`. Every run writes timestamped raw JSON and a Markdown table to
`benchmarks/results/`. Use smaller sweeps such as `--concurrency 1,5` or
`--sizes 1024,10240` for a quick local check; the defaults are the Milestone 3
target matrix.
