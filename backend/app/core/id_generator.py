import ulid


def new_prefixed_ulid(prefix: str) -> str:
    if not prefix.endswith("_"):
        raise ValueError("ID prefix must end with an underscore")
    return f"{prefix}{ulid.new()}"


def new_request_id() -> str:
    return new_prefixed_ulid("req_")
