import csv
import io
import zipfile

import pytest
from openpyxl import Workbook

from app.core.exceptions import ApplicationError
from app.modules.batch_jobs.parser import HEADERS, parse_product_import


def _row(**overrides: object) -> list[object]:
    values: dict[str, object] = {
        "product_key": "P001",
        "product_name": "通勤双肩包",
        "subtitle": "防泼水",
        "category_id": "cat_01TEST",
        "brand_id": "",
        "merchant_sku_code": "BAG-BLACK",
        "sku_name": "黑色标准版",
        "spec_values_json": '[{"name":"颜色","value":"黑色"}]',
        "sale_price_amount": 19900,
        "market_price_amount": 22900,
        "currency": "CNY",
        "weight_grams": 650,
        "barcode": "6900000000000",
        "initial_stock": 100,
    }
    values.update(overrides)
    return [values[name] for name in HEADERS]


def _csv_payload(*rows: list[object]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return output.getvalue().encode()


def test_csv_parser_accepts_grouped_product_skus() -> None:
    payload = _csv_payload(
        _row(),
        _row(
            merchant_sku_code="BAG-WHITE",
            sku_name="白色标准版",
            spec_values_json='[{"name":"颜色","value":"白色"}]',
        ),
    )

    parsed = parse_product_import(payload, "text/csv")

    assert len(parsed.rows) == 2
    assert parsed.rows[0].sale_price_amount == 19900
    assert parsed.rows[1].spec_values == [{"name": "颜色", "value": "白色"}]
    assert parsed.problems == ()


def test_csv_parser_invalidates_whole_product_group_on_conflict() -> None:
    payload = _csv_payload(
        _row(),
        _row(
            product_name="冲突名称",
            merchant_sku_code="BAG-WHITE",
            spec_values_json='[{"name":"颜色","value":"白色"}]',
        ),
    )

    parsed = parse_product_import(payload, "text/csv")

    assert parsed.rows == ()
    assert {problem.code for problem in parsed.problems} == {
        "IMPORT_PRODUCT_FIELDS_CONFLICT",
        "IMPORT_PRODUCT_GROUP_INVALID",
    }


def test_xlsx_parser_accepts_integer_valued_numeric_cells() -> None:
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.append(HEADERS)
    worksheet.append(_row(sale_price_amount=19900.0, market_price_amount=22900.0))
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()

    parsed = parse_product_import(
        output.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    assert len(parsed.rows) == 1
    assert parsed.rows[0].sale_price_amount == 19900


def test_xlsx_parser_rejects_external_links_before_opening_workbook() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("xl/externalLinks/externalLink1.xml", "<externalLink />")

    with pytest.raises(ApplicationError) as caught:
        parse_product_import(
            output.getvalue(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    assert caught.value.code == "IMPORT_XLSX_ACTIVE_CONTENT_FORBIDDEN"


def test_parser_rejects_non_versioned_headers() -> None:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow((*HEADERS[:-1], "stock"))
    writer.writerow(_row())

    with pytest.raises(ApplicationError) as caught:
        parse_product_import(output.getvalue().encode(), "text/csv")

    assert caught.value.code == "IMPORT_SCHEMA_MISMATCH"
