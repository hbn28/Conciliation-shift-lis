from __future__ import annotations

from app.application.ports.repositories import ReconciliationRepository
from app.domain.entities import Reconciliation


class ManageHistory:
    def __init__(self, repository: ReconciliationRepository):
        self.repository = repository

    def get(self, result_id: str) -> Reconciliation | None:
        return self.repository.get(result_id)

    def list(
        self, unit_id: int | None = None,
        date_from: str | None = None, date_to: str | None = None,
    ) -> list[Reconciliation]:
        return self.repository.list(unit_id, date_from, date_to)

    def list_conciliated_authorizations(self, unit_id: int | None = None) -> set[str]:
        return self.repository.list_conciliated_authorizations(unit_id)

    def list_conciliated_installments(self, unit_id: int | None = None) -> set[tuple[str, str]]:
        return self.repository.list_conciliated_installments(unit_id)

    def get_authorization_marks(self, result_id: str) -> dict[str, bool]:
        return self.repository.get_authorization_marks(result_id)

    def set_authorization_mark(
        self, result_id: str, autorizacao: str, conciliado: bool,
        data_vencimento: str = "", valor: str = "",
    ) -> None:
        self.repository.set_authorization_mark(result_id, autorizacao, conciliado, data_vencimento, valor)

    def delete(self, result_id: str) -> list[str]:
        return self.repository.delete(result_id)
