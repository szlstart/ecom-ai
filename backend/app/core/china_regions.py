from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import cast


@dataclass(frozen=True)
class ChinaRegion:
    province_code: str
    province_name: str
    city_code: str
    city_name: str
    district_code: str
    district_name: str

    @property
    def label(self) -> str:
        names = [self.province_name, self.city_name, self.district_name]
        return " ".join(
            name
            for index, name in enumerate(names)
            if name != "市辖区" and name not in names[:index]
        )


class ChinaRegionResolutionError(ValueError):
    pass


@lru_cache(maxsize=1)
def _area_data() -> dict[str, dict[str, str]]:
    payload = json.loads(
        files("app.resources").joinpath("china-area-data.json").read_text(encoding="utf-8")
    )
    if not isinstance(payload, dict):
        raise RuntimeError("china area reference data is invalid")
    return cast(dict[str, dict[str, str]], payload)


def resolve_china_region(value: str) -> ChinaRegion:
    parts = [part for part in re.split(r"[\s/、,，>]+", value.strip()) if part]
    if len(parts) not in {2, 3}:
        raise ChinaRegionResolutionError("地区必须依次写明省份、城市和区或县。")
    data = _area_data()
    province_code, province_name = _resolve_child(data.get("86", {}), parts[0], "省份")
    city_options = data.get(province_code, {})
    municipality_city = _municipality_child(city_options)
    if len(parts) == 2:
        if municipality_city is None:
            raise ChinaRegionResolutionError("地区必须依次写明省份、城市和区或县。")
        city_code, city_name = municipality_city
        district_value = parts[1]
    elif municipality_city is not None and _normalize(parts[1]) == _normalize(province_name):
        city_code, city_name = municipality_city
        district_value = parts[2]
    else:
        city_code, city_name = _resolve_child(city_options, parts[1], "城市")
        district_value = parts[2]
    district_code, district_name = _resolve_child(
        data.get(city_code, {}), district_value, "区或县"
    )
    return ChinaRegion(
        province_code=province_code,
        province_name=province_name,
        city_code=city_code,
        city_name=city_name,
        district_code=district_code,
        district_name=district_name,
    )


def region_label(province_code: str, city_code: str, district_code: str) -> str:
    data = _area_data()
    names = [
        data.get("86", {}).get(province_code, province_code),
        data.get(province_code, {}).get(city_code, city_code),
        data.get(city_code, {}).get(district_code, district_code),
    ]
    return " ".join(
        name for index, name in enumerate(names) if name != "市辖区" and name not in names[:index]
    )


def _resolve_child(options: dict[str, str], value: str, level: str) -> tuple[str, str]:
    if not options:
        raise ChinaRegionResolutionError(f"当前{level}没有可用的下级地区数据。")
    normalized = _normalize(value)
    exact = [(code, name) for code, name in options.items() if code == value or name == value]
    if len(exact) == 1:
        return exact[0]
    fuzzy = [(code, name) for code, name in options.items() if _normalize(name) == normalized]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        raise ChinaRegionResolutionError(f"{level}“{value}”不唯一，请填写完整名称。")
    raise ChinaRegionResolutionError(f"没有在上级地区内找到{level}“{value}”。")


def _municipality_child(options: dict[str, str]) -> tuple[str, str] | None:
    if len(options) != 1:
        return None
    item = next(iter(options.items()))
    return item if item[1] == "市辖区" else None


def _normalize(value: str) -> str:
    return re.sub(
        r"(?:壮族|回族|维吾尔)?自治区$|特别行政区$|自治州$|地区$|省$|市$|区$|县$",
        "",
        re.sub(r"\s+", "", value),
    )
