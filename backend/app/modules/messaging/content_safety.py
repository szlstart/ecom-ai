import unicodedata


def blocks_message(text: str) -> bool:
    """Apply the synchronous fail-closed rules shared by user and support send paths."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    forbidden = ("javascript:", "<script", "\x00", "begin private key")
    return any(item in normalized for item in forbidden)
