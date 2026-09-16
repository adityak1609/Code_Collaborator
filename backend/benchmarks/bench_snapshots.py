"""Measure snapshot create/restore latency across realistic document sizes."""

from __future__ import annotations

import argparse
import asyncio
import time

import pycrdt

from benchmarks.common import (
    DEFAULT_API_URL,
    authenticated_client,
    create_session,
    write_results,
)


def state_for_size(size: int) -> bytes:
    doc = pycrdt.Doc()
    doc["monaco"] = pycrdt.Text()
    doc["monaco"].insert(0, "x" * size)
    return doc.get_update()


async def run(api_url: str, sizes: list[int]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    client = await authenticated_client(api_url)
    try:
        for requested_size in sizes:
            session_id = await create_session(client, f"Snapshot {requested_size}")
            state = state_for_size(requested_size)
            saved = await client.post(
                f"/sessions/{session_id}/save",
                content=state,
                headers={"Content-Type": "application/octet-stream"},
            )
            saved.raise_for_status()

            started = time.perf_counter()
            created = await client.post(
                f"/sessions/{session_id}/snapshots",
                json={"label": f"{requested_size}-byte document"},
            )
            create_ms = (time.perf_counter() - started) * 1000
            created.raise_for_status()
            snapshot = created.json()

            started = time.perf_counter()
            restored = await client.post(
                f"/sessions/{session_id}/snapshots/{snapshot['id']}/restore"
            )
            restore_ms = (time.perf_counter() - started) * 1000
            restored.raise_for_status()
            rows.append(
                {
                    "document_bytes": requested_size,
                    "snapshot_size_bytes": snapshot["size_bytes"],
                    "snapshot_create_ms": round(create_ms, 3),
                    "snapshot_restore_ms": round(restore_ms, 3),
                }
            )
    finally:
        await client.aclose()
    return rows


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--sizes", default="1024,10240,102400,1048576")
    args = parser.parse_args()
    sizes = [int(value) for value in args.sizes.split(",")]
    rows = await run(args.api_url, sizes)
    paths = write_results(
        "snapshot_benchmark",
        rows,
        [
            "document_bytes",
            "snapshot_size_bytes",
            "snapshot_create_ms",
            "snapshot_restore_ms",
        ],
    )
    print(f"Wrote {paths[0]} and {paths[1]}")


if __name__ == "__main__":
    asyncio.run(main())
