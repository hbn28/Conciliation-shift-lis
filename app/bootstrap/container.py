from app.adapters.outbound.pandas_reconciliation import PandasReconciliationEngine
from app.adapters.outbound.sqlite_repository import (
    SQLiteReconciliationRepository,
    SQLiteRepository,
    SQLiteUnitRepository,
)
from app.application.use_cases.history import ManageHistory
from app.application.use_cases.process_reconciliation import ProcessReconciliation
from app.application.use_cases.units import ManageUnits


class Container:
    def __init__(self):
        database = SQLiteRepository()
        self.unit_repository = SQLiteUnitRepository(database)
        self.reconciliation_repository = SQLiteReconciliationRepository(database)
        self.reconciliation_engine = PandasReconciliationEngine()
        self.units = ManageUnits(self.unit_repository)
        self.history = ManageHistory(self.reconciliation_repository)
        self.process_reconciliation = ProcessReconciliation(
            self.unit_repository,
            self.reconciliation_repository,
            self.reconciliation_engine,
        )


container = Container()
