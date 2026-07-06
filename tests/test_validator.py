import pandas as pd

from app.adapters.outbound.spreadsheets.normalizer import normalize_dataframe
from app.adapters.outbound.spreadsheets.validator import WEAK_ALERT_CODES, validate_shift


def test_shift_duplicate_and_missing_fields():
    raw = pd.DataFrame([
        {"autorizacao": "55040", "nsu": "123", "valor": "10,00", "parcela": "1", "numero de parcelas": "1", "bandeira": "Visa", "modalidade": "Crédito", "status": "paga"},
        {"autorizacao": "55040", "nsu": "123", "valor": "10,00", "parcela": "1", "numero de parcelas": "1", "bandeira": "Visa", "modalidade": "Crédito", "status": "paga"},
    ])
    normalized = normalize_dataframe(raw, "shift")
    quality, issues = validate_shift(normalized)
    assert "PAGAMENTO_DUPLICADO" in issues[0]
    assert "AUTORIZACAO_DUPLICADA" in issues[1]
    assert not quality.empty


def test_valor_zerado_nao_marcado_quando_cancelado():
    raw = pd.DataFrame([
        {
            "autorizacao": "619531", "nsu": "172769140", "valor": "0",
            "parcela": "1", "numero de parcelas": "1", "bandeira": "Visa",
            "modalidade": "Crédito", "status": "desagendada",
            "cancelamento": "cancelamento",
        },
    ])
    normalized = normalize_dataframe(raw, "shift")
    _, issues = validate_shift(normalized)
    assert "VALOR_ZERADO" not in issues[0]


def test_valor_zerado_marcado_quando_nao_cancelado():
    raw = pd.DataFrame([
        {
            "autorizacao": "619531", "nsu": "172769140", "valor": "0",
            "parcela": "1", "numero de parcelas": "1", "bandeira": "Visa",
            "modalidade": "Crédito", "status": "paga",
        },
    ])
    normalized = normalize_dataframe(raw, "shift")
    _, issues = validate_shift(normalized)
    assert "VALOR_ZERADO" in issues[0]


def test_autorizacao_duplicada_isolada_e_alerta_fraco():
    # Mesma autorização, mas NSU e valor diferentes: não é o mesmo pagamento
    # repetido, então não deve virar erro cadastral forte.
    raw = pd.DataFrame([
        {"autorizacao": "123456", "nsu": "111", "valor": "10,00", "parcela": "1", "numero de parcelas": "1", "bandeira": "Visa", "modalidade": "Crédito", "status": "paga"},
        {"autorizacao": "123456", "nsu": "222", "valor": "20,00", "parcela": "1", "numero de parcelas": "1", "bandeira": "Visa", "modalidade": "Crédito", "status": "paga"},
    ])
    normalized = normalize_dataframe(raw, "shift")
    quality, issues = validate_shift(normalized)
    assert "AUTORIZACAO_DUPLICADA" in issues[0]
    assert "PAGAMENTO_DUPLICADO" not in issues[0]
    assert "AUTORIZACAO_DUPLICADA" in WEAK_ALERT_CODES
    linha = quality[quality["problema"] == "AUTORIZACAO_DUPLICADA"].iloc[0]
    assert linha["classificacao"] == "ALERTA_CADASTRAL_SHIFT"


def test_pagamento_duplicado_por_chave_composta_e_erro_forte():
    raw = pd.DataFrame([
        {"autorizacao": "123456", "nsu": "111", "valor": "10,00", "parcela": "1", "numero de parcelas": "1", "bandeira": "Visa", "modalidade": "Crédito", "status": "paga"},
        {"autorizacao": "123456", "nsu": "111", "valor": "10,00", "parcela": "1", "numero de parcelas": "1", "bandeira": "Visa", "modalidade": "Crédito", "status": "paga"},
    ])
    normalized = normalize_dataframe(raw, "shift")
    quality, issues = validate_shift(normalized)
    assert "PAGAMENTO_DUPLICADO" in issues[0]
    linha = quality[quality["problema"] == "PAGAMENTO_DUPLICADO"].iloc[0]
    assert linha["classificacao"] == "ERRO_CADASTRAL_SHIFT"
