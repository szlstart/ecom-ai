#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python}"

"${PYTHON_BIN}" -m piptools compile \
  --allow-unsafe \
  --generate-hashes \
  --strip-extras \
  --resolver=backtracking \
  --output-file=requirements/base.txt \
  pyproject.toml

"${PYTHON_BIN}" -m piptools compile \
  --allow-unsafe \
  --generate-hashes \
  --strip-extras \
  --resolver=backtracking \
  --extra=dev \
  --output-file=requirements/dev.txt \
  pyproject.toml
