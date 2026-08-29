# API unavailable

1. Confirm `/health/live` and `/health/ready` from inside the backend network.
2. Check API restart count and structured logs by request/trace ID.
3. Verify MySQL, PostgreSQL and Redis health without printing credentials.
4. Roll back the latest application release if health failed after deployment.
5. Do not bypass migrations or restore writes until consistency checks pass.
