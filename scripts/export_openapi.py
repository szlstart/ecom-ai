from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("ECOM_READINESS_CHECKS_ENABLED", "false")

from app.core.config import get_settings
from app.main import create_app


def main() -> None:
    get_settings.cache_clear()
    target = Path(__file__).resolve().parents[1] / "docs" / "openapi-v1.json"
    target.write_text(
        json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(target)


if __name__ == "__main__":
    main()
