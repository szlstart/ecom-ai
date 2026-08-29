from __future__ import annotations

import json
import subprocess
import sys

BACKEND_SERVICES = (
    "api",
    "file-worker",
    "batch-worker",
    "order-timeout-worker",
    "payment-reconcile-worker",
    "logistics-sync-worker",
    "admin-approval-worker",
    "realtime-outbox-worker",
    "agent-runtime-worker",
    "knowledge-indexer",
    "ai-memory-cleanup-worker",
)


def main() -> int:
    image_ids: dict[str, str] = {}
    build_shas: dict[str, str] = {}
    missing: list[str] = []
    for service in BACKEND_SERVICES:
        container_id = _run("docker", "compose", "ps", "-q", service).strip()
        if not container_id:
            missing.append(service)
            continue
        details = json.loads(_run("docker", "inspect", container_id))[0]
        image_ids[service] = str(details["Image"])
        environment = _environment(details["Config"].get("Env", []))
        build_shas[service] = environment.get("ECOM_BUILD_SHA", "")

    problems: list[str] = []
    if missing:
        problems.append("missing containers: " + ", ".join(missing))
    if len(set(image_ids.values())) > 1:
        problems.append("backend services are running different image IDs")
    if len(set(build_shas.values())) > 1 or not all(build_shas.values()):
        problems.append("backend services are running different or empty build SHAs")
    if problems:
        print("Runtime version check failed: " + "; ".join(problems), file=sys.stderr)
        return 1

    build_sha = next(iter(build_shas.values()), "unknown")
    image_id = next(iter(image_ids.values()), "unknown")
    print(f"Runtime versions match: build={build_sha} image={image_id[:19]}")
    return 0


def _run(*command: str) -> str:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _environment(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if separator:
            result[key] = value
    return result


if __name__ == "__main__":
    raise SystemExit(main())
