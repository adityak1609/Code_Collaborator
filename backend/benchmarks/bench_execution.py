"""Measure execution queue and sandbox throughput under concurrent load."""

from __future__ import annotations

import argparse
import asyncio
import time

from benchmarks.common import (
    DEFAULT_API_URL,
    authenticated_client,
    create_session,
    percentile,
    write_results,
)

TERMINAL = {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"}


async def one_sweep(client, concurrency: int) -> dict[str, object]:
    session_id = await create_session(client, f"Execution benchmark {concurrency}")
    submitted = time.perf_counter()
    responses = await asyncio.gather(
        *(client.post(f"/sessions/{session_id}/run") for _ in range(concurrency))
    )
    for response in responses:
        response.raise_for_status()
    executions = {
        response.json()["id"]: {"first_running": None} for response in responses
    }
    completed: list[dict[str, object]] = []
    deadline = time.monotonic() + 180
    while executions and time.monotonic() < deadline:
        now = time.perf_counter()
        for execution_id in list(executions):
            response = await client.get(
                f"/sessions/{session_id}/executions/{execution_id}"
            )
            response.raise_for_status()
            record = response.json()
            if (
                record["status"] == "RUNNING"
                and executions[execution_id]["first_running"] is None
            ):
                executions[execution_id]["first_running"] = now
            if record["status"] in TERMINAL:
                record["observed_finished"] = now
                record["first_running"] = executions[execution_id]["first_running"]
                completed.append(record)
                del executions[execution_id]
        if executions:
            await asyncio.sleep(0.05)
    if executions:
        raise TimeoutError(f"{len(executions)} executions did not finish")

    elapsed = max(record["observed_finished"] for record in completed) - submitted
    starts = [
        (record["first_running"] - submitted) * 1000
        for record in completed
        if record["first_running"] is not None
    ]
    return {
        "concurrency": concurrency,
        "jobs": len(completed),
        "throughput_jobs_per_second": round(len(completed) / elapsed, 3),
        "queue_to_start_p50_ms": percentile(starts, 0.50),
        "queue_to_start_p95_ms": percentile(starts, 0.95),
        "total_p95_ms": percentile(
            [float(item["elapsed_ms"] or 0) for item in completed], 0.95
        ),
        "timeout_rate_percent": round(
            100
            * sum(item["status"] == "TIMEOUT" for item in completed)
            / len(completed),
            2,
        ),
    }


async def run(api_url: str, levels: list[int]) -> list[dict[str, object]]:
    client = await authenticated_client(api_url)
    try:
        return [await one_sweep(client, level) for level in levels]
    finally:
        await client.aclose()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--concurrency", default="1,5,10,20")
    args = parser.parse_args()
    rows = await run(
        args.api_url, [int(value) for value in args.concurrency.split(",")]
    )
    paths = write_results(
        "execution_benchmark",
        rows,
        [
            "concurrency",
            "jobs",
            "throughput_jobs_per_second",
            "queue_to_start_p50_ms",
            "queue_to_start_p95_ms",
            "total_p95_ms",
            "timeout_rate_percent",
        ],
    )
    print(f"Wrote {paths[0]} and {paths[1]}")


if __name__ == "__main__":
    asyncio.run(main())
