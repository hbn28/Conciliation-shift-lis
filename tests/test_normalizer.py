from datetime import date
from decimal import Decimal

import pytest

from app.adapters.outbound.spreadsheets.normalizer import (
    normalize_authorization,
    normalize_date,
    normalize_int,
    normalize_money,
    normalize_nsu,
)


@pytest.mark.parametrize(("raw", "expected"), [
    ("055040", "055040"),
    ("55040", "055040"),
    ("5621", "005621"),
    ("055040.0", "055040"),
    (None, None),
    ("M02305", "M02305"),
    ("0685866", "685866"),
    ("0000123", "000123"),
])
def test_normalize_authorization(raw, expected):
    assert normalize_authorization(raw) == expected


def test_authorization_too_long():
    with pytest.raises(ValueError, match="AUTORIZACAO_TAMANHO_INVALIDO"):
        normalize_authorization("1234567")


@pytest.mark.parametrize(("raw", "expected"), [
    ("00123456", "123456"),
    ("123456.0", "123456"),
    (None, None),
    ("  789  ", "789"),
])
def test_normalize_nsu(raw, expected):
    assert normalize_nsu(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [
    ("R$ 1.234,56", Decimal("1234.56")),
    ("1.234,56", Decimal("1234.56")),
    ("1234,56", Decimal("1234.56")),
    ("1234.56", Decimal("1234.56")),
    ("1,234.56", Decimal("1234.56")),
    ("10,50", Decimal("10.50")),
    ("10.50", Decimal("10.50")),
    ("-10,50", Decimal("-10.50")),
    ("(10,50)", Decimal("-10.50")),
    ("", None),
    (None, None),
    ("abc", None),
])
def test_normalize_money(raw, expected):
    assert normalize_money(raw) == expected


@pytest.mark.parametrize(("raw", "part", "expected"), [
    ("1/3", "first", 1),
    ("1/3", "second", 3),
    ("01/03", "first", 1),
    ("01/03", "second", 3),
    ("1 de 3", "first", 1),
    ("1 de 3", "second", 3),
    ("1", "first", 1),
    ("1", "second", 1),
    (None, "first", None),
])
def test_normalize_int_parcela(raw, part, expected):
    assert normalize_int(raw, part) == expected


@pytest.mark.parametrize(("raw", "expected"), [
    ("2026-07-01", date(2026, 7, 1)),
    ("01/07/2026", date(2026, 7, 1)),
    ("01-07-2026", date(2026, 7, 1)),
    (None, None),
    ("", None),
    (45840, date(2025, 7, 2)),  # número serial do Excel
])
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected
