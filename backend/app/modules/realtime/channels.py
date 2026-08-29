from __future__ import annotations


def user_channel(environment: str, user_no: str) -> str:
    return f"ecom:{environment}:realtime:user:{user_no}:v1"


def admin_user_channel(environment: str, user_no: str) -> str:
    return f"ecom:{environment}:realtime:admin-user:{user_no}:v1"


def admin_platform_channel(environment: str) -> str:
    return f"ecom:{environment}:realtime:admin-scope:platform:0:v1"


def admin_store_channel(environment: str, store_id: int) -> str:
    return f"ecom:{environment}:realtime:admin-scope:store:{store_id}:v1"
