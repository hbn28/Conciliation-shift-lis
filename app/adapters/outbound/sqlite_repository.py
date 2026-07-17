from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.domain.entities import (
    ImportedFile,
    Reconciliation,
    ReconciliationSave,
    Unit,
)
from app.domain.exceptions import ConflictError, NotFoundError


DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "storage" / "conciliacao.db"


class SQLiteRepository:
    """Adaptador SQLite para as portas de unidades e conciliações."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(
            db_path or os.getenv("DATABASE_PATH", str(DEFAULT_DB_PATH))
        )
        self.init_schema()

    @contextmanager
    def _connection(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self):
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS unidades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    codigo TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    nome TEXT NOT NULL,
                    estabelecimento TEXT,
                    ativa INTEGER NOT NULL DEFAULT 1,
                    criado_em TEXT NOT NULL,
                    atualizado_em TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conciliacoes (
                    id TEXT PRIMARY KEY,
                    unidade_id INTEGER NOT NULL REFERENCES unidades(id),
                    data_conciliacao TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resumo_json TEXT NOT NULL,
                    criado_em TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS importacoes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conciliacao_id TEXT NOT NULL
                        REFERENCES conciliacoes(id) ON DELETE CASCADE,
                    origem TEXT NOT NULL,
                    categoria TEXT NOT NULL CHECK (categoria IN ('REDE', 'SHIFT')),
                    nome_original TEXT NOT NULL,
                    caminho_arquivo TEXT NOT NULL,
                    quantidade_linhas INTEGER NOT NULL,
                    criado_em TEXT NOT NULL,
                    UNIQUE (conciliacao_id, origem)
                );
                CREATE TABLE IF NOT EXISTS autorizacoes_conciliadas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conciliacao_id TEXT NOT NULL
                        REFERENCES conciliacoes(id) ON DELETE CASCADE,
                    unidade_id INTEGER NOT NULL REFERENCES unidades(id),
                    autorizacao TEXT NOT NULL COLLATE NOCASE,
                    data_vencimento TEXT NOT NULL DEFAULT '',
                    valor TEXT NOT NULL DEFAULT '',
                    conciliado INTEGER NOT NULL DEFAULT 1,
                    origem_registro TEXT NOT NULL DEFAULT 'MANUAL',
                    criado_em TEXT NOT NULL,
                    atualizado_em TEXT NOT NULL,
                    UNIQUE (unidade_id, autorizacao, data_vencimento, valor)
                );
                CREATE INDEX IF NOT EXISTS idx_conciliacoes_unidade_data
                    ON conciliacoes(unidade_id, data_conciliacao);
                CREATE INDEX IF NOT EXISTS idx_importacoes_conciliacao
                    ON importacoes(conciliacao_id);
                CREATE INDEX IF NOT EXISTS idx_autorizacoes_conciliacao
                    ON autorizacoes_conciliadas(conciliacao_id);
            """)
            # Migração idempotente: bancos criados antes deste campo não têm
            # a coluna "empresa_shift" na tabela "unidades".
            existing_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(unidades)").fetchall()
            }
            if "empresa_shift" not in existing_columns:
                conn.execute("ALTER TABLE unidades ADD COLUMN empresa_shift TEXT")
            import_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(importacoes)").fetchall()
            }
            if "categoria" not in import_columns:
                conn.executescript("""
                    ALTER TABLE importacoes RENAME TO importacoes_old;
                    CREATE TABLE importacoes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conciliacao_id TEXT NOT NULL
                            REFERENCES conciliacoes(id) ON DELETE CASCADE,
                        origem TEXT NOT NULL,
                        categoria TEXT NOT NULL CHECK (categoria IN ('REDE', 'SHIFT')),
                        nome_original TEXT NOT NULL,
                        caminho_arquivo TEXT NOT NULL,
                        quantidade_linhas INTEGER NOT NULL,
                        criado_em TEXT NOT NULL,
                        UNIQUE (conciliacao_id, origem)
                    );
                    INSERT INTO importacoes (
                        id, conciliacao_id, origem, categoria, nome_original,
                        caminho_arquivo, quantidade_linhas, criado_em
                    )
                    SELECT
                        id, conciliacao_id, origem, origem, nome_original,
                        caminho_arquivo, quantidade_linhas, criado_em
                    FROM importacoes_old;
                    DROP TABLE importacoes_old;
                    CREATE INDEX IF NOT EXISTS idx_importacoes_conciliacao
                        ON importacoes(conciliacao_id);
                """)
            auth_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(autorizacoes_conciliadas)").fetchall()
            }
            if not auth_columns:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS autorizacoes_conciliadas (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conciliacao_id TEXT NOT NULL
                            REFERENCES conciliacoes(id) ON DELETE CASCADE,
                        unidade_id INTEGER NOT NULL REFERENCES unidades(id),
                        autorizacao TEXT NOT NULL COLLATE NOCASE,
                        data_vencimento TEXT NOT NULL DEFAULT '',
                        valor TEXT NOT NULL DEFAULT '',
                        conciliado INTEGER NOT NULL DEFAULT 1,
                        origem_registro TEXT NOT NULL DEFAULT 'MANUAL',
                        criado_em TEXT NOT NULL,
                        atualizado_em TEXT NOT NULL,
                        UNIQUE (unidade_id, autorizacao, data_vencimento, valor)
                    );
                    CREATE INDEX IF NOT EXISTS idx_autorizacoes_conciliacao
                        ON autorizacoes_conciliadas(conciliacao_id);
                """)
                auth_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(autorizacoes_conciliadas)").fetchall()
                }
            if "origem_registro" not in auth_columns:
                # DEFAULT 'AUTO' já é aplicado pelo ALTER TABLE a todas as
                # linhas existentes; não é necessário um UPDATE adicional.
                conn.execute(
                    "ALTER TABLE autorizacoes_conciliadas ADD COLUMN origem_registro TEXT NOT NULL DEFAULT 'AUTO'"
                )
                auth_columns.add("origem_registro")
            if "data_vencimento" not in auth_columns:
                # Banco criado antes de diferenciarmos parcelas da mesma
                # autorização pelo vencimento. Precisa reconstruir a tabela
                # porque o UNIQUE antigo não incluía data_vencimento
                # (ALTER TABLE não permite mudar constraints no SQLite).
                # Marcações antigas (sem vencimento) viram data_vencimento=''.
                conn.executescript("""
                    ALTER TABLE autorizacoes_conciliadas RENAME TO autorizacoes_conciliadas_old;
                    CREATE TABLE autorizacoes_conciliadas (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conciliacao_id TEXT NOT NULL
                            REFERENCES conciliacoes(id) ON DELETE CASCADE,
                        autorizacao TEXT NOT NULL COLLATE NOCASE,
                        data_vencimento TEXT NOT NULL DEFAULT '',
                        conciliado INTEGER NOT NULL DEFAULT 1,
                        origem_registro TEXT NOT NULL DEFAULT 'MANUAL',
                        criado_em TEXT NOT NULL,
                        atualizado_em TEXT NOT NULL,
                        UNIQUE (conciliacao_id, autorizacao, data_vencimento)
                    );
                    INSERT INTO autorizacoes_conciliadas (
                        conciliacao_id, autorizacao, data_vencimento,
                        conciliado, origem_registro, criado_em, atualizado_em
                    )
                    SELECT
                        conciliacao_id, autorizacao, '',
                        conciliado, origem_registro, criado_em, atualizado_em
                    FROM autorizacoes_conciliadas_old;
                    DROP TABLE autorizacoes_conciliadas_old;
                    CREATE INDEX IF NOT EXISTS idx_autorizacoes_conciliacao
                        ON autorizacoes_conciliadas(conciliacao_id);
                """)
                auth_columns.add("data_vencimento")
            if "unidade_id" not in auth_columns:
                # Bug real: a marcação era escopada só por conciliacao_id,
                # então reprocessar os mesmos arquivos (gera um conciliacao_id
                # novo) fazia a marcação "sumir" mesmo sendo a mesma unidade e
                # a mesma venda. O escopo correto é por unidade (mesma lógica
                # já usada em list_conciliated_authorizations) — units
                # diferentes com autorização igual continuam isoladas, mas
                # reprocessar os arquivos da mesma unidade preserva a marca.
                # unidade_id é preenchido a partir de conciliacoes.unidade_id.
                conn.executescript("""
                    ALTER TABLE autorizacoes_conciliadas RENAME TO autorizacoes_conciliadas_old;
                    CREATE TABLE autorizacoes_conciliadas (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conciliacao_id TEXT NOT NULL
                            REFERENCES conciliacoes(id) ON DELETE CASCADE,
                        unidade_id INTEGER NOT NULL REFERENCES unidades(id),
                        autorizacao TEXT NOT NULL COLLATE NOCASE,
                        data_vencimento TEXT NOT NULL DEFAULT '',
                        conciliado INTEGER NOT NULL DEFAULT 1,
                        origem_registro TEXT NOT NULL DEFAULT 'MANUAL',
                        criado_em TEXT NOT NULL,
                        atualizado_em TEXT NOT NULL,
                        UNIQUE (unidade_id, autorizacao, data_vencimento)
                    );
                    INSERT OR IGNORE INTO autorizacoes_conciliadas (
                        conciliacao_id, unidade_id, autorizacao, data_vencimento,
                        conciliado, origem_registro, criado_em, atualizado_em
                    )
                    SELECT
                        old.conciliacao_id, c.unidade_id, old.autorizacao, old.data_vencimento,
                        old.conciliado, old.origem_registro, old.criado_em, old.atualizado_em
                    FROM autorizacoes_conciliadas_old old
                    JOIN conciliacoes c ON c.id = old.conciliacao_id
                    ORDER BY old.atualizado_em DESC;
                    DROP TABLE autorizacoes_conciliadas_old;
                    CREATE INDEX IF NOT EXISTS idx_autorizacoes_conciliacao
                        ON autorizacoes_conciliadas(conciliacao_id);
                """)
                auth_columns.add("unidade_id")
            if "valor" not in auth_columns:
                # A mesma autorização + vencimento pode, em casos raros,
                # corresponder a parcelas diferentes com valores diferentes
                # (ou o valor de uma parcela do Shift ser a soma de mais de
                # uma transação com a mesma autorização/vencimento). O valor
                # bruto entra na identidade da parcela para diferenciar esses
                # casos. Marcações antigas (sem valor registrado) viram
                # valor='' — continuam batendo entre si, mas não com marcações
                # novas da mesma autorização/vencimento que já tragam valor.
                conn.executescript("""
                    ALTER TABLE autorizacoes_conciliadas RENAME TO autorizacoes_conciliadas_old;
                    CREATE TABLE autorizacoes_conciliadas (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conciliacao_id TEXT NOT NULL
                            REFERENCES conciliacoes(id) ON DELETE CASCADE,
                        unidade_id INTEGER NOT NULL REFERENCES unidades(id),
                        autorizacao TEXT NOT NULL COLLATE NOCASE,
                        data_vencimento TEXT NOT NULL DEFAULT '',
                        valor TEXT NOT NULL DEFAULT '',
                        conciliado INTEGER NOT NULL DEFAULT 1,
                        origem_registro TEXT NOT NULL DEFAULT 'MANUAL',
                        criado_em TEXT NOT NULL,
                        atualizado_em TEXT NOT NULL,
                        UNIQUE (unidade_id, autorizacao, data_vencimento, valor)
                    );
                    INSERT INTO autorizacoes_conciliadas (
                        id, conciliacao_id, unidade_id, autorizacao, data_vencimento, valor,
                        conciliado, origem_registro, criado_em, atualizado_em
                    )
                    SELECT
                        id, conciliacao_id, unidade_id, autorizacao, data_vencimento, '',
                        conciliado, origem_registro, criado_em, atualizado_em
                    FROM autorizacoes_conciliadas_old;
                    DROP TABLE autorizacoes_conciliadas_old;
                    CREATE INDEX IF NOT EXISTS idx_autorizacoes_conciliacao
                        ON autorizacoes_conciliadas(conciliacao_id);
                """)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _unit(row: sqlite3.Row) -> Unit:
        columns = row.keys()
        return Unit(
            id=row["id"], codigo=row["codigo"], nome=row["nome"],
            estabelecimento=row["estabelecimento"], ativa=bool(row["ativa"]),
            criado_em=row["criado_em"], atualizado_em=row["atualizado_em"],
            empresa_shift=row["empresa_shift"] if "empresa_shift" in columns else None,
        )

    def list(self, active_only: bool = False) -> list[Unit]:
        query = "SELECT * FROM unidades"
        if active_only:
            query += " WHERE ativa = 1"
        query += " ORDER BY nome COLLATE NOCASE"
        with self._connection() as conn:
            return [self._unit(row) for row in conn.execute(query).fetchall()]

    def get(self, unit_id: int) -> Unit | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM unidades WHERE id = ?", (unit_id,)
            ).fetchone()
        return self._unit(row) if row else None

    def create(
        self, codigo: str, nome: str, estabelecimento: str | None = None,
        empresa_shift: str | None = None,
    ) -> int:
        codigo, nome = codigo.strip(), nome.strip()
        if not codigo or not nome:
            raise ConflictError("Código e nome são obrigatórios.")
        now = self._now()
        try:
            with self._connection() as conn:
                cursor = conn.execute(
                    """INSERT INTO unidades
                       (codigo, nome, estabelecimento, empresa_shift, ativa, criado_em, atualizado_em)
                       VALUES (?, ?, ?, ?, 1, ?, ?)""",
                    (codigo, nome, estabelecimento.strip() if estabelecimento else None,
                     empresa_shift.strip() if empresa_shift else None, now, now),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Já existe uma unidade com esse código.") from exc

    def update(
        self, unit_id: int, codigo: str, nome: str,
        estabelecimento: str | None, ativa: bool,
        empresa_shift: str | None = None,
    ) -> None:
        codigo, nome = codigo.strip(), nome.strip()
        if not codigo or not nome:
            raise ConflictError("Código e nome são obrigatórios.")
        try:
            with self._connection() as conn:
                cursor = conn.execute(
                    """UPDATE unidades SET codigo = ?, nome = ?,
                       estabelecimento = ?, empresa_shift = ?, ativa = ?, atualizado_em = ?
                       WHERE id = ?""",
                    (codigo, nome, estabelecimento.strip() if estabelecimento else None,
                     empresa_shift.strip() if empresa_shift else None,
                     int(ativa), self._now(), unit_id),
                )
                if cursor.rowcount == 0:
                    raise NotFoundError("Unidade não encontrada.")
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Já existe uma unidade com esse código.") from exc

    def delete(self, unit_id: int) -> None:
        with self._connection() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM conciliacoes WHERE unidade_id = ?", (unit_id,)
            ).fetchone()[0]
            if total:
                raise ConflictError(
                    "A unidade possui conciliações. Desative-a para preservar o histórico."
                )
            cursor = conn.execute("DELETE FROM unidades WHERE id = ?", (unit_id,))
            if cursor.rowcount == 0:
                raise NotFoundError("Unidade não encontrada.")

    def save(self, data: ReconciliationSave) -> None:
        now = self._now()
        summary = {
            key: float(value) if hasattr(value, "as_tuple") else value
            for key, value in data.resumo.items()
        }
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO conciliacoes
                   (id, unidade_id, data_conciliacao, status, resumo_json, criado_em)
                   VALUES (?, ?, ?, 'CONCLUIDA', ?, ?)""",
                (data.id, data.unidade_id, data.data_conciliacao,
                 json.dumps(summary, ensure_ascii=False), now),
            )
            conn.executemany(
                """INSERT INTO importacoes
                   (conciliacao_id, origem, categoria, nome_original, caminho_arquivo,
                    quantidade_linhas, criado_em)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        data.id,
                        item.origem,
                        item.categoria,
                        item.nome_original,
                        item.caminho_arquivo,
                        item.quantidade_linhas,
                        now,
                    )
                    for item in data.arquivos_importados
                ],
            )

    @staticmethod
    def _imported_file(row: sqlite3.Row) -> ImportedFile:
        return ImportedFile(
            id=row["id"], conciliacao_id=row["conciliacao_id"],
            origem=row["origem"], categoria=row["categoria"],
            nome_original=row["nome_original"],
            caminho_arquivo=row["caminho_arquivo"],
            quantidade_linhas=row["quantidade_linhas"],
            criado_em=row["criado_em"],
        )

    @staticmethod
    def _mark_key(autorizacao: str, data_vencimento: str, valor: str = "") -> str:
        # A mesma autorização pode se repetir em parcelas diferentes de uma
        # venda parcelada (vencimentos diferentes) ou, mais raramente, com o
        # mesmo vencimento mas valores diferentes; por isso a chave de
        # marcação combina autorização + vencimento + valor (campos "" quando
        # desconhecidos/legado).
        return f"{autorizacao}|{data_vencimento or ''}|{valor or ''}"

    def _unidade_id_da_conciliacao(self, conn: sqlite3.Connection, result_id: str) -> int | None:
        row = conn.execute(
            "SELECT unidade_id FROM conciliacoes WHERE id = ?", (result_id,)
        ).fetchone()
        return row["unidade_id"] if row else None

    def get_authorization_marks(self, result_id: str) -> dict[str, bool]:
        # Escopado por unidade (não por conciliacao_id): reprocessar os
        # mesmos arquivos da mesma unidade gera um conciliacao_id novo a
        # cada vez, então escopar só pela execução individual fazia a
        # marcação "sumir" ao reabrir/reprocessar. A unidade é o escopo
        # correto — a mesma lógica já usada em list_conciliated_authorizations
        # — e continua isolando unidades diferentes que por coincidência
        # tenham a mesma autorização.
        with self._connection() as conn:
            unidade_id = self._unidade_id_da_conciliacao(conn, result_id)
            if unidade_id is None:
                return {}
            rows = conn.execute(
                """
                SELECT autorizacao, data_vencimento, valor, MAX(conciliado) AS conciliado
                FROM autorizacoes_conciliadas
                WHERE origem_registro = 'MANUAL' AND unidade_id = ?
                GROUP BY autorizacao, data_vencimento, valor
                """,
                (unidade_id,),
            ).fetchall()
        return {
            self._mark_key(row["autorizacao"], row["data_vencimento"], row["valor"]): bool(row["conciliado"])
            for row in rows
        }

    def set_authorization_mark(
        self, result_id: str, autorizacao: str, conciliado: bool,
        data_vencimento: str = "", valor: str = "",
    ) -> None:
        now = self._now()
        with self._connection() as conn:
            unidade_id = self._unidade_id_da_conciliacao(conn, result_id)
            if unidade_id is None:
                raise NotFoundError("Conciliação não encontrada.")
            conn.execute(
                """
                INSERT INTO autorizacoes_conciliadas
                    (conciliacao_id, unidade_id, autorizacao, data_vencimento, valor, conciliado, origem_registro, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, 'MANUAL', ?, ?)
                ON CONFLICT(unidade_id, autorizacao, data_vencimento, valor)
                DO UPDATE SET
                    conciliacao_id = excluded.conciliacao_id,
                    conciliado = excluded.conciliado,
                    origem_registro = 'MANUAL',
                    atualizado_em = excluded.atualizado_em
                """,
                (result_id, unidade_id, autorizacao, data_vencimento or "", valor or "", int(conciliado), now, now),
            )

    def _reconciliation(
        self,
        row: sqlite3.Row,
        files: dict[str, ImportedFile] | None = None,
        marks: dict[str, bool] | None = None,
    ) -> Reconciliation:
        return Reconciliation(
            id=row["id"], unidade_id=row["unidade_id"],
            data_conciliacao=row["data_conciliacao"], status=row["status"],
            resumo=json.loads(row["resumo_json"]), criado_em=row["criado_em"],
            unidade_codigo=row["unidade_codigo"],
            unidade_nome=row["unidade_nome"], arquivos=files or {},
            autorizacoes_marcadas=marks or {},
        )

    def get_reconciliation(self, result_id: str) -> Reconciliation | None:
        with self._connection() as conn:
            row = conn.execute(
                """SELECT c.*, u.codigo AS unidade_codigo, u.nome AS unidade_nome
                   FROM conciliacoes c JOIN unidades u ON u.id = c.unidade_id
                   WHERE c.id = ?""",
                (result_id,),
            ).fetchone()
            if not row:
                return None
            files = {
                item["origem"]: self._imported_file(item)
                for item in conn.execute(
                    "SELECT * FROM importacoes WHERE conciliacao_id = ?",
                    (result_id,),
                ).fetchall()
            }
            # Mesma lógica de get_authorization_marks: escopado por unidade,
            # vencimento e valor, para não vazar marcação entre unidades
            # diferentes nem entre parcelas diferentes da mesma autorização,
            # mas persistir entre reprocessamentos dos mesmos arquivos.
            marks = {
                self._mark_key(item["autorizacao"], item["data_vencimento"], item["valor"]): bool(item["conciliado"])
                for item in conn.execute(
                    """
                    SELECT autorizacao, data_vencimento, valor, MAX(conciliado) AS conciliado
                    FROM autorizacoes_conciliadas
                    WHERE origem_registro = 'MANUAL' AND unidade_id = ?
                    GROUP BY autorizacao, data_vencimento, valor
                    """,
                    (row["unidade_id"],),
                ).fetchall()
            }
        return self._reconciliation(row, files, marks)

    def list_reconciliations(
        self, unit_id: int | None = None,
        date_from: str | None = None, date_to: str | None = None,
    ) -> list[Reconciliation]:
        clauses, params = [], []
        if unit_id:
            clauses.append("c.unidade_id = ?")
            params.append(unit_id)
        if date_from:
            clauses.append("c.data_conciliacao >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("c.data_conciliacao <= ?")
            params.append(date_to)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"""SELECT c.*, u.codigo AS unidade_codigo, u.nome AS unidade_nome
                    FROM conciliacoes c JOIN unidades u ON u.id = c.unidade_id
                    {where}
                    ORDER BY c.data_conciliacao DESC, c.criado_em DESC""",
                params,
            ).fetchall()
        return [self._reconciliation(row) for row in rows]

    def list_conciliated_authorizations(self, unit_id: int | None = None) -> set[str]:
        query = """
            SELECT DISTINCT a.autorizacao FROM autorizacoes_conciliadas a
            WHERE a.conciliado = 1 AND a.origem_registro = 'MANUAL'
        """
        params: list = []
        if unit_id is not None:
            query += """
                AND a.conciliacao_id IN (
                    SELECT id FROM conciliacoes WHERE unidade_id = ?
                )
            """
            params.append(unit_id)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return {row["autorizacao"] for row in rows if row["autorizacao"]}

    def list_conciliated_installments(self, unit_id: int | None = None) -> set[tuple[str, str]]:
        # Granularidade por parcela (autorização + vencimento): diferente de
        # list_conciliated_authorizations, que marca a autorização inteira
        # como conciliada assim que UMA parcela é marcada. Usado onde é
        # importante não confundir "uma parcela conciliada" com "todas as
        # parcelas dessa autorização conciliadas" (ex.: /verificar-conciliados,
        # que soma várias parcelas da Rede sob a mesma autorização).
        query = """
            SELECT DISTINCT a.autorizacao, a.data_vencimento FROM autorizacoes_conciliadas a
            WHERE a.conciliado = 1 AND a.origem_registro = 'MANUAL'
        """
        params: list = []
        if unit_id is not None:
            query += """
                AND a.conciliacao_id IN (
                    SELECT id FROM conciliacoes WHERE unidade_id = ?
                )
            """
            params.append(unit_id)
        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return {
            (row["autorizacao"], row["data_vencimento"] or "")
            for row in rows if row["autorizacao"]
        }

    def delete_reconciliation(self, result_id: str) -> list[str]:
        with self._connection() as conn:
            paths = [
                row["caminho_arquivo"]
                for row in conn.execute(
                    "SELECT caminho_arquivo FROM importacoes WHERE conciliacao_id = ?",
                    (result_id,),
                ).fetchall()
            ]
            cursor = conn.execute(
                "DELETE FROM conciliacoes WHERE id = ?", (result_id,)
            )
            if cursor.rowcount == 0:
                raise NotFoundError("Conciliação não encontrada.")
        return paths


class SQLiteUnitRepository:
    def __init__(self, database: SQLiteRepository):
        self.database = database

    def list(self, active_only=False):
        return self.database.list(active_only)

    def get(self, unit_id):
        return self.database.get(unit_id)

    def create(self, codigo, nome, estabelecimento=None, empresa_shift=None):
        return self.database.create(codigo, nome, estabelecimento, empresa_shift)

    def update(self, unit_id, codigo, nome, estabelecimento, ativa, empresa_shift=None):
        return self.database.update(unit_id, codigo, nome, estabelecimento, ativa, empresa_shift)

    def delete(self, unit_id):
        return self.database.delete(unit_id)


class SQLiteReconciliationRepository:
    def __init__(self, database: SQLiteRepository):
        self.database = database

    def save(self, data):
        return self.database.save(data)

    def get(self, result_id):
        return self.database.get_reconciliation(result_id)

    def list(self, unit_id=None, date_from=None, date_to=None):
        return self.database.list_reconciliations(unit_id, date_from, date_to)

    def list_conciliated_authorizations(self, unit_id: int | None = None) -> set[str]:
        return self.database.list_conciliated_authorizations(unit_id)

    def list_conciliated_installments(self, unit_id: int | None = None) -> set[tuple[str, str]]:
        return self.database.list_conciliated_installments(unit_id)

    def delete(self, result_id):
        return self.database.delete_reconciliation(result_id)

    def get_authorization_marks(self, result_id: str) -> dict[str, bool]:
        return self.database.get_authorization_marks(result_id)

    def set_authorization_mark(
        self, result_id: str, autorizacao: str, conciliado: bool,
        data_vencimento: str = "", valor: str = "",
    ) -> None:
        return self.database.set_authorization_mark(result_id, autorizacao, conciliado, data_vencimento, valor)
