import pytest

from app.core.china_regions import (
    ChinaRegionResolutionError,
    region_label,
    resolve_china_region,
)


def test_resolve_china_region_requires_a_valid_parent_child_path() -> None:
    region = resolve_china_region("广东省/深圳市/南山区")
    assert (region.province_code, region.city_code, region.district_code) == (
        "440000",
        "440300",
        "440305",
    )
    assert region.label == "广东省 深圳市 南山区"
    assert region_label("110000", "110100", "110105") == "北京市 朝阳区"

    with pytest.raises(ChinaRegionResolutionError, match="上级地区"):
        resolve_china_region("广东省/北京市/朝阳区")


def test_resolve_china_region_accepts_unambiguous_short_names() -> None:
    region = resolve_china_region("广东 深圳 南山")
    assert region.district_code == "440305"
