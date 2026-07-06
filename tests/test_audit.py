from decimal import Decimal

import pandas as pd

from app.adapters.outbound.spreadsheets.audit import partition_valid_rows


def test_linha_invalida_vai_para_descartes_com_motivo():
    frame = pd.DataFrame([
        {
            "_row": 12,
            "empresa": "CENTRALLAB (Cz)",
            "raw_autorizacao": "",
            "autorizacao": None,
            "valor_bruto": Decimal("10.00"),
        },
        {
            "_row": 13,
            "empresa": "CENTRALLAB (Cz)",
            "raw_autorizacao": "123456",
            "autorizacao": "123456",
            "valor_bruto": Decimal("20.00"),
        },
    ])
    valid, discarded = partition_valid_rows(frame, "SHIFT")
    assert len(valid) == 1
    assert discarded.loc[0, "numero_linha_original"] == 12
    assert discarded.loc[0, "motivo_descarte"] == "AUTORIZACAO_AUSENTE"
