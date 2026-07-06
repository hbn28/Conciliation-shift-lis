from pathlib import Path

import pandas as pd
import pytest

from app.adapters.outbound.spreadsheets.file_reader import read_file


def test_reads_semicolon_csv_and_drops_empty_export_columns(tmp_path: Path):
    path = tmp_path / "shift.csv"
    path.write_text(
        "NSU;autorização;valor bruto;Coluna1;Coluna2\n"
        "001;55040;1,25;;\n",
        encoding="utf-8-sig",
    )
    frame = read_file(path)
    assert list(frame.columns) == ["NSU", "autorização", "valor bruto"]
    assert frame.loc[0, "NSU"] == "001"


def test_empty_csv_returns_empty_dataframe(tmp_path: Path):
    path = tmp_path / "vazio.csv"
    path.write_text("", encoding="utf-8")
    frame = read_file(path)
    assert frame.empty


def test_extensao_invalida_gera_erro_claro(tmp_path: Path):
    path = tmp_path / "arquivo.txt"
    path.write_text("qualquer coisa", encoding="utf-8")
    with pytest.raises(ValueError, match="Formato não suportado"):
        read_file(path)


def test_aba_inexistente_gera_erro_claro(tmp_path: Path):
    path = tmp_path / "rede.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame({"autorizacao": ["1"]}).to_excel(writer, sheet_name="pagamentos", index=False)
    with pytest.raises(ValueError, match="não encontrada"):
        read_file(path, sheet_name="aba_que_nao_existe")
