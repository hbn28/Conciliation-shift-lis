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
        resumo={
            "total_linhas_rede": 2,
            "total_linhas_shift": 2,
            "autorizacoes_conciliadas": ["001234"],
        },
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
    assert history.list_conciliated_authorizations() == set()

    history.set_authorization_mark("a" * 32, "001234", True)
    assert history.list_conciliated_authorizations() == {"001234"}
    # Chave combina autorização + vencimento + valor (campos vazios quando
    # não informados) para diferenciar parcelas da mesma autorização.
    assert history.get("a" * 32).autorizacoes_marcadas == {"001234||": True}

    # Regressão: reprocessar os mesmos arquivos da MESMA unidade gera um
    # conciliacao_id novo ("b" * 32), mas a marcação deve persistir — ela é
    # escopada por unidade (UNIQUE(unidade_id, autorizacao, data_vencimento)),
    # não pela execução/upload individual. Sem isso, marcar uma autorização e
    # reabrir a mesma unidade/arquivos fazia a marcação "sumir".
    database.save(ReconciliationSave(
        id="b" * 32,
        unidade_id=unit_id,
        data_conciliacao="2025-12-30",
        resumo={
            "total_linhas_rede": 1,
            "total_linhas_shift": 1,
            "autorizacoes_conciliadas": ["001234"],
        },
        arquivos_importados=[],
    ))
    assert history.get("b" * 32).autorizacoes_marcadas == {"001234||": True}

    # Regressão: unidades DIFERENTES com a mesma autorização continuam
    # isoladas — marcar não deve vazar entre unidades.
    outra_unidade_id = units.create("XX", "Outra Unidade")
    database.save(ReconciliationSave(
        id="c" * 32,
        unidade_id=outra_unidade_id,
        data_conciliacao="2025-12-30",
        resumo={
            "total_linhas_rede": 1,
            "total_linhas_shift": 1,
            "autorizacoes_conciliadas": ["001234"],
        },
        arquivos_importados=[],
    ))
    assert history.get("c" * 32).autorizacoes_marcadas == {}

    # Regressão: a mesma autorização com vencimentos diferentes (parcelas
    # diferentes de uma venda parcelada) deve ser marcada separadamente —
    # marcar a parcela de um vencimento não deve marcar a de outro. E marcar
    # via "b" (mesma unidade de "a") deve refletir também ao consultar "a".
    history.set_authorization_mark("b" * 32, "001234", True, "2026-01-10")
    marcadas_b = history.get("b" * 32).autorizacoes_marcadas
    assert marcadas_b.get("001234|2026-01-10|") is True
    assert marcadas_b.get("001234|2026-02-10|") is None
    history.set_authorization_mark("b" * 32, "001234", True, "2026-02-10")
    history.set_authorization_mark("b" * 32, "001234", False, "2026-01-10")
    marcadas_a = history.get("a" * 32).autorizacoes_marcadas
    marcadas_b = history.get("b" * 32).autorizacoes_marcadas
    assert marcadas_a == marcadas_b
    assert marcadas_b.get("001234|2026-01-10|") is False
    assert marcadas_b.get("001234|2026-02-10|") is True

    # Regressão: a mesma autorização e o mesmo vencimento, mas com valores
    # diferentes, são parcelas distintas — marcar uma não marca a outra.
    history.set_authorization_mark("b" * 32, "005555", True, "2026-03-10", "100.00")
    marcadas_b = history.get("b" * 32).autorizacoes_marcadas
    assert marcadas_b.get("005555|2026-03-10|100.00") is True
    assert marcadas_b.get("005555|2026-03-10|200.00") is None
    history.set_authorization_mark("b" * 32, "005555", True, "2026-03-10", "200.00")
    marcadas_b = history.get("b" * 32).autorizacoes_marcadas
    assert marcadas_b.get("005555|2026-03-10|100.00") is True
    assert marcadas_b.get("005555|2026-03-10|200.00") is True

    history.delete("c" * 32)
    units.delete(outra_unidade_id)

    try:
        units.delete(unit_id)
    except DomainError as exc:
        assert "Desative-a" in str(exc)
    else:
        raise AssertionError("Unidade com histórico não deve ser excluída")

    history.delete("a" * 32)
    history.delete("b" * 32)
    units.delete(unit_id)
    assert units.get(unit_id) is None
