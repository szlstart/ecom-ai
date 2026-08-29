from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, Field

PASSWORD_MAX_UTF8_BYTES = 4_096


def validate_password_input(value: str) -> str:
    """Apply transport-safety bounds before any password reaches Argon2."""
    if any(character.isspace() for character in value):
        raise ValueError("密码不能包含空格、换行或其他空白字符")
    if len(value.encode("utf-8")) > PASSWORD_MAX_UTF8_BYTES:
        raise ValueError(f"密码不能超过 {PASSWORD_MAX_UTF8_BYTES} 个 UTF-8 字节")
    return value


PasswordInput = Annotated[
    str,
    Field(min_length=1, max_length=PASSWORD_MAX_UTF8_BYTES),
    AfterValidator(validate_password_input),
]
