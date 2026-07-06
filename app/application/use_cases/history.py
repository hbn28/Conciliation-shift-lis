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

    def delete(self, result_id: str) -> list[str]:
        return self.repository.delete(result_id)

