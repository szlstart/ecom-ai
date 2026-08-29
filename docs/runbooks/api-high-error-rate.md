# API high error rate

1. Group 5xx errors by bounded route template and error code.
2. Correlate the deployment, database pool, queue lag and provider status.
3. Disable the affected feature flag or external adapter when degradation is isolated.
4. Preserve request/trace IDs; never copy tokens, message bodies or tracking numbers into tickets.
