#!/usr/bin/env python3
"""Generate the packaged permission seed catalog from the normative registry."""

from __future__ import annotations

import hashlib
import pprint
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "permission_registry.yaml"
TARGET = ROOT / "backend" / "app" / "generated" / "permission_catalog.py"


def main() -> None:
    source_bytes = SOURCE.read_bytes()
    data = yaml.safe_load(source_bytes)
    defaults = data.get("defaults", {})
    permissions: list[dict[str, object]] = []
    for raw in data["permissions"]:
        item = {**defaults, **raw}
        item["description"] = f"{item['resource']} {item['action']}"
        permissions.append(item)

    rendered = (
        '"""Generated from docs/permission_registry.yaml; do not edit manually."""\n\n'
        f'SOURCE_SHA256 = "{hashlib.sha256(source_bytes).hexdigest()}"\n'
        f"PERMISSIONS = {pprint.pformat(permissions, width=100, sort_dicts=False)}\n"
    )
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
