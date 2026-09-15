"""Process one queued execution by ID for worker diagnostics."""

import argparse
import asyncio
import sys
import uuid

from app.execution.worker import ExecutionWorker


async def run(execution_id: uuid.UUID) -> None:
    worker = ExecutionWorker()
    try:
        await worker.process(execution_id)
    finally:
        await worker.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("execution_id", type=uuid.UUID)
    args = parser.parse_args()
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(run(args.execution_id))
    else:
        asyncio.run(run(args.execution_id))


if __name__ == "__main__":
    main()
