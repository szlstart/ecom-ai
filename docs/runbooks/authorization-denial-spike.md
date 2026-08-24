# Authorization denial spike

1. Split denials by bounded reason, deployment version, endpoint and admin/user audience.
2. Confirm whether the increase is an attack, stale permission version, bad rollout or expected policy enforcement.
3. Never weaken Scope, ownership, confirmation or MFA rules to reduce the alert rate.
4. For suspected abuse, tighten edge rate limits and preserve Request/Trace/Audit references without raw tokens.
5. For a policy regression, roll back the immutable policy version and invalidate affected sessions if required.
6. Escalate any successful cross-user/cross-store access or unconfirmed write as P0 immediately.
