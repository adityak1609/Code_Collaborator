"""Benchmark real y-websocket update fan-out at increasing concurrency."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import time
from urllib.parse import quote

import docker
import psutil
import pycrdt
from websockets.asyncio.client import ClientConnection, connect

from app.collaboration.protocol import (
    SYNC_STEP1,
    SyncMessage,
    encode_sync_step2,
    encode_sync_update,
    parse_message,
)
from benchmarks.common import (
    DEFAULT_API_URL,
    authenticated_client,
    create_session,
    percentile,
    write_results,
)


def backend_rss_mb(
    explicit_pid: int | None,
    container_name: str | None,
) -> float | None:
    processes = (
        [psutil.Process(explicit_pid)] if explicit_pid else psutil.process_iter()
    )
    for process in processes:
        try:
            command = " ".join(process.cmdline())
            if explicit_pid or ("uvicorn" in command and "app.main:app" in command):
                return round(process.memory_info().rss / (1024 * 1024), 2)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    if container_name:
        client = None
        try:
            client = docker.from_env()
            stats = client.containers.get(container_name).stats(stream=False)
            memory = stats.get("memory_stats", {})
            usage = int(memory.get("usage", 0))
            cache = int(memory.get("stats", {}).get("inactive_file", 0))
            return round(max(0, usage - cache) / (1024 * 1024), 2)
        except Exception:
            return None
        finally:
            if client is not None:
                client.close()
    return None


async def open_client(url: str) -> ClientConnection:
    websocket = await connect(url, max_size=16 * 1024 * 1024)
    first = await asyncio.wait_for(websocket.recv(), timeout=10)
    if not isinstance(first, bytes):
        raise RuntimeError("Server did not send a binary Yjs handshake")
    message = parse_message(first)
    if not isinstance(message, SyncMessage) or message.subtype != SYNC_STEP1:
        raise RuntimeError("Server did not begin with Yjs sync step 1")
    empty = pycrdt.Doc()
    empty["monaco"] = pycrdt.Text()
    await websocket.send(encode_sync_step2(empty.get_update(message.payload)))
    return websocket


async def one_sweep(
    ws_url: str,
    token: str,
    session_id: str,
    concurrency: int,
    iterations: int,
    server_pid: int | None,
    container_name: str | None,
) -> dict[str, object]:
    url = f"{ws_url}/ws/{session_id}?token={quote(token)}"
    attempts = await asyncio.gather(
        *(open_client(url) for _ in range(concurrency)), return_exceptions=True
    )
    clients = [item for item in attempts if isinstance(item, ClientConnection)]
    if not clients:
        raise RuntimeError("No benchmark WebSocket connections succeeded")

    send_times: dict[bytes, float] = {}
    delivered_to: dict[bytes, set[int]] = {}
    latencies: list[float] = []
    done = asyncio.Event()
    expected = len(clients) * iterations * len(clients)

    async def receive(index: int, websocket: ClientConnection) -> None:
        async for frame in websocket:
            if not isinstance(frame, bytes):
                continue
            with contextlib.suppress(Exception):
                message = parse_message(frame)
                if not isinstance(message, SyncMessage):
                    continue
                started = send_times.get(message.payload)
                if started is None:
                    continue
                receivers = delivered_to.setdefault(message.payload, set())
                if index in receivers:
                    continue
                receivers.add(index)
                latencies.append((time.perf_counter() - started) * 1000)
                if sum(map(len, delivered_to.values())) >= expected:
                    done.set()

    listeners = [
        asyncio.create_task(receive(index, websocket))
        for index, websocket in enumerate(clients)
    ]

    async def send_updates(index: int, websocket: ClientConnection) -> None:
        doc = pycrdt.Doc()
        doc["monaco"] = pycrdt.Text()
        text: pycrdt.Text = doc["monaco"]
        for sequence in range(iterations):
            before = doc.get_state()
            text.insert(len(str(text)), f"{index}:{sequence};")
            update = doc.get_update(before)
            send_times[update] = time.perf_counter()
            await websocket.send(encode_sync_update(update))
            await asyncio.sleep(0.1)

    try:
        peak_memory = await asyncio.to_thread(
            backend_rss_mb, server_pid, container_name
        )
        await asyncio.gather(
            *(send_updates(index, websocket) for index, websocket in enumerate(clients))
        )
        final_memory = await asyncio.to_thread(
            backend_rss_mb, server_pid, container_name
        )
        memory_samples = [
            value for value in (peak_memory, final_memory) if value is not None
        ]
        peak_memory = max(memory_samples) if memory_samples else None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(done.wait(), timeout=10)
    finally:
        for listener in listeners:
            listener.cancel()
        await asyncio.gather(*listeners, return_exceptions=True)
        await asyncio.gather(*(client.close() for client in clients))

    delivered = sum(map(len, delivered_to.values()))
    return {
        "concurrent_users": concurrency,
        "connect_rate_percent": round(100 * len(clients) / concurrency, 2),
        "delivery_rate_percent": round(100 * delivered / expected, 2),
        "latency_p50_ms": percentile(latencies, 0.50),
        "latency_p95_ms": percentile(latencies, 0.95),
        "latency_p99_ms": percentile(latencies, 0.99),
        "server_memory_mb": peak_memory if peak_memory is not None else "n/a",
    }


async def run(
    api_url: str,
    levels: list[int],
    iterations: int,
    server_pid: int | None,
    container_name: str | None,
) -> list[dict[str, object]]:
    client = await authenticated_client(api_url)
    try:
        session_id = await create_session(client, "WebSocket benchmark")
        token = client.headers["Authorization"].removeprefix("Bearer ")
        ws_url = api_url.replace("https://", "wss://").replace("http://", "ws://")
        return [
            await one_sweep(
                ws_url,
                token,
                session_id,
                level,
                iterations,
                server_pid,
                container_name,
            )
            for level in levels
        ]
    finally:
        await client.aclose()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--concurrency", default="10,25,50,100")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument(
        "--server-pid", type=int, default=os.getenv("BENCHMARK_SERVER_PID")
    )
    parser.add_argument(
        "--server-container",
        default=os.getenv("BENCHMARK_SERVER_CONTAINER", "code_collaborator-backend-1"),
    )
    args = parser.parse_args()
    rows = await run(
        args.api_url,
        [int(value) for value in args.concurrency.split(",")],
        args.iterations,
        int(args.server_pid) if args.server_pid else None,
        args.server_container,
    )
    paths = write_results(
        "websocket_benchmark",
        rows,
        [
            "concurrent_users",
            "connect_rate_percent",
            "delivery_rate_percent",
            "latency_p50_ms",
            "latency_p95_ms",
            "latency_p99_ms",
            "server_memory_mb",
        ],
    )
    print(f"Wrote {paths[0]} and {paths[1]}")


if __name__ == "__main__":
    asyncio.run(main())
