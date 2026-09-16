"""Shared benchmark setup, percentile, and result writers."""

from __future__ import annotations

import json
import math
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

RESULTS_DIR = Path(__file__).parent / "results"
DEFAULT_API_URL = os.getenv("BENCHMARK_API_URL", "http://127.0.0.1:8000")
PASSWORD = "Benchmark-password-2026"


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(percentile_value * len(ordered)) - 1)
    return round(ordered[max(0, index)], 3)


async def authenticated_client(api_url: str = DEFAULT_API_URL) -> httpx.AsyncClient:
    suffix = uuid.uuid4().hex[:10]
    username = f"bench_{suffix}"
    email = f"{username}@example.com"
    client = httpx.AsyncClient(base_url=api_url, timeout=60)
    response = await client.post(
        "/auth/register",
        json={"username": username, "email": email, "password": PASSWORD},
    )
    response.raise_for_status()
    response = await client.post(
        "/auth/login", json={"username": username, "password": PASSWORD}
    )
    response.raise_for_status()
    client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
    return client


async def create_session(
    client: httpx.AsyncClient, name: str, language="python"
) -> str:
    response = await client.post("/sessions", json={"name": name, "language": language})
    response.raise_for_status()
    return response.json()["id"]


def write_results(
    name: str, rows: list[dict[str, object]], columns: list[str]
) -> tuple[Path, Path]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "benchmark": name,
        "generated_at": datetime.now(UTC).isoformat(),
        "results": rows,
    }
    json_path = RESULTS_DIR / f"{name}-{timestamp}.json"
    markdown_path = RESULTS_DIR / f"{name}-{timestamp}.md"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    headers = "| " + " | ".join(columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [f"## {name.replace('_', ' ').title()}  {timestamp}", "", headers, divider]
    for row in rows:
        lines.append(
            "| " + " | ".join(str(row.get(column, "")) for column in columns) + " |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path
