from app.bootstrap.default_knowledge import (
    _content_version,
    _document_no,
    _platform_sources,
)


def test_platform_knowledge_sources_are_packaged_and_stable() -> None:
    sources = _platform_sources()

    assert len(sources) == 4
    assert {item.scope_type for item in sources} == {"platform"}
    assert {item.scope_no for item in sources} == {"platform"}
    assert all(item.title.startswith("[系统] ") for item in sources)
    assert all(item.safe_text.strip() for item in sources)
    assert len({item.document_no for item in sources}) == len(sources)


def test_system_knowledge_ids_and_versions_are_content_addressed() -> None:
    assert _document_no("platform:规则.md") == _document_no("platform:规则.md")
    assert _document_no("platform:规则.md") != _document_no("store:规则.md")
    assert _content_version("版本一") == _content_version("版本一")
    assert _content_version("版本一") != _content_version("版本二")
