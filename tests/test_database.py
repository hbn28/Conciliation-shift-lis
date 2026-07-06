from app.adapters.outbound.sqlite_repository import (
    SQLiteReconciliationRepository,
    SQLiteRepository,
    SQLiteUnitRepository,
)
from app.application.use_cases.history import ManageHistory
from app.application.use_cases.units import ManageUnits
from app.domain.entities import ImportedFile, ReconciliationSave
from app.domain.exceptions import DomainError


def test_unit_and_reconciliation_history(tmp_path):
    database = SQLiteRepository(tmp_path / "test.db")
    units = ManageUnits(SQLiteUnitRepository(database))
    history = ManageHistory(SQLiteReconciliationRepository(database))
    unit_id = units.create("JZ", "Centrallab JZ", "69269769", "CENTRALLAB (Jn)")
    unidade = units.get(unit_id)
    assert unidade.nome == "Centrallab JZ"
    assert unidade.empresa_shift == "CENTRALLAB (Jn)"

    units.update(unit_id, "JZ", "Centrallab JZ", "69269769", True, "CENTRALLAB JZ (Jn)")
    assert units.get(unit_id).empresa_shift == "CENTRALLAB JZ (Jn)"

    database.save(ReconciliationSave(
        id="a" * 32,
        unidade_id=unit_id,
        data_conciliacao="2025-12-29",
        resumo={"total_linhas_rede": 2, "total_linhas_shift": 2},
        arquivos_importados=[
            ImportedFile(
                id=0,
                conciliacao_id="a" * 32,
                origem="REDE_01",
                categoria="REDE",
                nome_original="rede-1.xlsx",
                caminho_arquivo=str(tmp_path / "rede-1.xlsx"),
                quantidade_linhas=2,
                criado_em="",
            ),
            ImportedFile(
                id=0,
                conciliacao_id="a" * 32,
                origem="REDE_02",
                categoria="REDE",
                nome_original="rede-2.xlsx",
                caminho_arquivo=str(tmp_path / "rede-2.xlsx"),
                quantidade_linhas=3,
                criado_em="",
            ),
            ImportedFile(
                id=0,
                conciliacao_id="a" * 32,
                origem="SHIFT",
                categoria="SHIFT",
                nome_original="shift.csv",
                caminho_arquivo=str(tmp_path / "shift.csv"),
                quantidade_linhas=2,
                criado_em="",
            ),
        ],
    ))
    records = history.list(unit_id, "2025-12-29", "2025-12-29")
    assert len(records) == 1
    assert records[0].unidade_codigo == "JZ"
    saved = history.get("a" * 32)
    assert saved is not None
    assert saved.arquivos["REDE_01"].categoria == "REDE"
    assert saved.arquivos["REDE_02"].quantidade_linhas == 3
    assert saved.arquivos["SHIFT"].categoria == "SHIFT"

    try:
        units.delete(unit_id)
    except DomainError as exc:
        assert "Desative-a" in str(exc)
    else:
        raise AssertionError("Unidade com histórico não deve ser excluída")

    history.delete("a" * 32)
    units.delete(unit_id)
    assert units.get(unit_id) is None
