import pytest
from fastapi.routing import APIRoute

from app.core.exceptions import ApplicationError
from app.main import create_app
from app.modules.catalog.content_sanitizer import sanitize_content


def test_platform_content_operations_are_registered() -> None:
    operations = {
        route.operation_id
        for route in create_app().routes
        if isinstance(route, APIRoute) and route.operation_id
    }
    assert {
        "AdminContent_List",
        "AdminContent_Create",
        "AdminContent_Get",
        "AdminContent_Update",
        "AdminContent_Publish",
        "AdminContent_Withdraw",
        "HomeBanner_ListPublished",
        "PlatformAnnouncement_ListPublished",
        "HelpArticle_ListPublished",
        "HelpArticle_GetPublished",
        "FooterContent_GetPublished",
        "AboutContent_GetPublished",
    } <= operations


def test_platform_html_uses_the_shared_strict_sanitizer() -> None:
    safe = sanitize_content("html", "<h2>帮助中心</h2><p>安全内容</p>")
    assert safe.safe_html is not None
    assert "帮助中心" in safe.safe_text
    with pytest.raises(ApplicationError):
        sanitize_content("html", '<p>安全内容</p><script>alert("x")</script>')
