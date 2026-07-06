from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pandas as pd

from app.application.models import UploadedSpreadsheet
from app.application.ports.reconciliation import EngineOutput
from app.adapters.outbound.spreadsheets.audit import (
    DISCARD_COLUMNS,
    build_audit_frame,
    discard_record,
    partition_valid_rows,
)
from app.adapters.outbound.spreadsheets.context_validator import (
    filter_shift_empresa,
    validate_rede_context,
    validate_shift_empresa,
)
from app.adapters.outbound.spreadsheets.file_reader import (
    is_shift_financial_report,
    read_file,
    read_file_with_metadata,
    read_shift_financial_report,
)
from app.adapters.outbound.spreadsheets.matcher import compare_rede_shift
from app.adapters.outbound.spreadsheets.normalizer import (
    normalize_dataframe,
    normalize_shift_financial_report,
)
from app.adapters.outbound.spreadsheets.reporter import generate_reports

REDE_DEDUP_FIELDS = [
    "autorizacao", "nsu", "valor_bruto", "valor_liquido",
    "data_venda", "data_recebimento", "parcela", "numero_parcelas",
    "bandeira", "modalidade",
]
REDE_DEDUP_CRITERIO = (
    "AUTORIZACAO+NSU+VALORES+DATAS+PARCELA+BANDEIRA+MODALIDADE"
)


def _prepare_rede_frame(
    frame: pd.DataFrame,
    arquivo: UploadedSpreadsheet,
    metadata: dict,
) -> pd.DataFrame:
    rede = normalize_dataframe(frame, "rede")
    rede["rede_arquivo_origem"] = arquivo.nome_original
    rede["rede_origem_id"] = arquivo.origem
    rede["rede_aba_origem"] = metadata.get("sheet_name")
    rede["rede_data_relatorio"] = metadata.get("report_date")
    rede["rede_linha_original"] = rede["_row"]
    rede["rede_duplicado_entre_arquivos"] = False
    rede["rede_arquivos_duplicados"] = None
    rede["criterio_deduplicacao_rede"] = None
    return rede


def _deduplicate_rede(
    frame: pd.DataFrame,
) -> tuple[pd.DataFrame, list[dict], int]:
    if frame.empty:
        return frame, [], 0
    grouped_indexes: dict[tuple, list[int]] = {}
    for idx, row in frame.iterrows():
        key = tuple(row.get(field) for field in REDE_DEDUP_FIELDS)
        grouped_indexes.setdefault(key, []).append(idx)

    keep_indexes: list[int] = []
    discard_records: list[dict] = []
    duplicated_between_files = 0
    for indexes in grouped_indexes.values():
        if len(indexes) == 1:
            keep_indexes.append(indexes[0])
            continue
        subset = frame.loc[indexes]
        arquivos = [
            value for value in subset["rede_arquivo_origem"].dropna().astype(str).tolist()
            if value
        ]
        if len(set(arquivos)) <= 1:
            keep_indexes.extend(indexes)
            continue
        keeper = indexes[0]
        keep_indexes.append(keeper)
        duplicated_between_files += len(indexes) - 1
        arquivos_duplicados = " | ".join(dict.fromkeys(arquivos))
        frame.at[keeper, "rede_duplicado_entre_arquivos"] = True
        frame.at[keeper, "rede_arquivos_duplicados"] = arquivos_duplicados
        frame.at[keeper, "criterio_deduplicacao_rede"] = REDE_DEDUP_CRITERIO
        for idx in indexes[1:]:
            row = frame.loc[idx]
            discard_records.append(discard_record(
                row,
                "REDE",
                "DUPLICIDADE_ENTRE_ARQUIVOS_REDE",
                f"Registro duplicado em múltiplos arquivos da Rede. Mantido: {frame.at[keeper, 'rede_arquivo_origem']}.",
            ))
    result = frame.loc[sorted(set(keep_indexes))].reset_index(drop=True)
    return result, discard_records, duplicated_between_files


class PandasReconciliationEngine:
    """Adaptador que integra arquivos tabulares ao caso de uso."""

    def execute(
        self,
        rede_files: list[UploadedSpreadsheet],
        shift_files: list[UploadedSpreadsheet],
        reconciliation_date: str,
        establishment: str | None,
        output_dir: Path,
        shift_empresa: str | None = None,
    ) -> EngineOutput:
        raw_rede_frames: list[pd.DataFrame] = []
        rede_frames: list[pd.DataFrame] = []
        rede_invalid_frames: list[pd.DataFrame] = []
        rede_rows_por_arquivo: dict[str, int] = {}
        linhas_lidas_por_arquivo: dict[str, int] = {}
        linhas_validas_por_arquivo: dict[str, int] = {}
        linhas_descartadas_por_arquivo: dict[str, int] = {}

        for arquivo in rede_files:
            raw_rede, metadata = read_file_with_metadata(arquivo.path, arquivo.sheet)
            linhas_lidas_por_arquivo[arquivo.nome_original] = len(raw_rede)
            rede_rows_por_arquivo[arquivo.origem] = len(raw_rede)
            prepared = _prepare_rede_frame(raw_rede, arquivo, metadata)
            rede_valid, rede_invalid = partition_valid_rows(prepared, "REDE")
            linhas_validas_por_arquivo[arquivo.nome_original] = len(rede_valid)
            linhas_descartadas_por_arquivo[arquivo.nome_original] = len(rede_invalid)
            raw_rede_frames.append(raw_rede)
            rede_frames.append(rede_valid)
            rede_invalid_frames.append(rede_invalid)

        raw_rede = pd.concat(raw_rede_frames, ignore_index=True) if raw_rede_frames else pd.DataFrame()
        rede = pd.concat(rede_frames, ignore_index=True) if rede_frames else pd.DataFrame()
        rede_invalid = pd.concat(rede_invalid_frames, ignore_index=True) if rede_invalid_frames else pd.DataFrame(columns=DISCARD_COLUMNS)
        rede, rede_dedup_discards, duplicated_between_files = _deduplicate_rede(rede)

        raw_shift_frames: list[pd.DataFrame] = []
        shift_frames: list[pd.DataFrame] = []
        shift_rows_por_arquivo: dict[str, int] = {}
        shift_report_discards: list[dict] = []
        shift_report_totais = {"total_linhas": 0, "linhas_cartao": 0, "linhas_ignoradas": 0}
        any_shift_report = False

        for arquivo in shift_files:
            if is_shift_financial_report(arquivo.path):
                any_shift_report = True
                raw_shift_arquivo, stats = read_shift_financial_report(
                    arquivo.path, payment_scope="card"
                )
                shift_arquivo = normalize_shift_financial_report(raw_shift_arquivo)
                shift_report_totais["total_linhas"] += stats["total_linhas"]
                shift_report_totais["linhas_cartao"] += stats["linhas_cartao"]
                shift_report_totais["linhas_ignoradas"] += stats["linhas_ignoradas"]
                shift_report_discards.extend(stats.get("descartes", []))
            else:
                raw_shift_arquivo = read_file(arquivo.path, arquivo.sheet)
                shift_arquivo = normalize_dataframe(raw_shift_arquivo, "shift")
            shift_rows_por_arquivo[arquivo.origem] = len(raw_shift_arquivo)
            raw_shift_frames.append(raw_shift_arquivo)
            shift_frames.append(shift_arquivo)

        raw_shift = pd.concat(raw_shift_frames, ignore_index=True) if raw_shift_frames else pd.DataFrame()
        shift = pd.concat(shift_frames, ignore_index=True) if shift_frames else pd.DataFrame()
        shift_report_stats = (
            {**shift_report_totais, "descartes": shift_report_discards}
            if any_shift_report else None
        )

        context = validate_rede_context(rede, reconciliation_date, establishment)
        context["alertas_empresa_shift"] = validate_shift_empresa(shift, shift_empresa)
        shift, company_filter_stats = filter_shift_empresa(shift, shift_empresa)
        context.update(company_filter_stats)
        shift, shift_invalid = partition_valid_rows(shift, "SHIFT")
        comparison = compare_rede_shift(rede, shift)
        comparison.resumo["shift_linhas_antes_filtro_empresa"] = company_filter_stats["antes_filtro_empresa"]
        comparison.resumo["shift_linhas_apos_filtro_empresa"] = company_filter_stats["apos_filtro_empresa"]
        if shift_report_stats:
            comparison.resumo["shift_linhas_relatorio_total"] = shift_report_stats["total_linhas"]
            comparison.resumo["shift_linhas_cartao"] = shift_report_stats["linhas_cartao"]
            comparison.resumo["shift_linhas_ignoradas_pix_outros"] = shift_report_stats["linhas_ignoradas"]

        discard_records = []
        if shift_report_stats:
            discard_records.extend(shift_report_stats.get("descartes", []))
        discard_records.extend(company_filter_stats.get("descartes", []))
        discard_records.extend(rede_dedup_discards)
        discards = pd.concat([
            pd.DataFrame(discard_records, columns=DISCARD_COLUMNS),
            shift_invalid,
            rede_invalid,
        ], ignore_index=True)

        comparison.resumo.update({
            "shift_total_linhas_lidas": (
                shift_report_stats["total_linhas"] if shift_report_stats else len(raw_shift)
            ),
            "shift_linhas_cartao": (
                shift_report_stats["linhas_cartao"] if shift_report_stats else len(raw_shift)
            ),
            "shift_linhas_unidade_filtrada": company_filter_stats["apos_filtro_empresa"],
            "shift_linhas_validas_para_conciliacao": len(shift),
            "shift_linhas_descartadas": int((discards["origem"] == "SHIFT").sum()),
            "shift_linhas_agrupadas": comparison.resumo.get("total_linhas_agrupadas_shift", 0),
            "shift_grupos_criados": comparison.resumo.get("total_agrupamentos_shift", 0),
            "shift_transacoes_enviadas_para_matching": comparison.resumo["total_linhas_shift"],
            "quantidade_arquivos_rede_importados": len(rede_files),
            "quantidade_arquivos_shift_importados": len(shift_files),
            "linhas_lidas_por_arquivo_shift": shift_rows_por_arquivo,
            "rede_total_linhas_lidas": len(raw_rede),
            "rede_linhas_pagamentos": len(raw_rede),
            "rede_linhas_validas_para_conciliacao": len(rede),
            "rede_linhas_descartadas": int((discards["origem"] == "REDE").sum()),
            "rede_transacoes_enviadas_para_matching": len(rede),
            "rede_duplicidades_entre_arquivos": duplicated_between_files,
            "transacoes_rede_consolidadas": len(rede),
            "linhas_lidas_por_arquivo_rede": linhas_lidas_por_arquivo,
            "linhas_validas_por_arquivo_rede": linhas_validas_por_arquivo,
            "linhas_descartadas_por_arquivo_rede": linhas_descartadas_por_arquivo,
        })
        comparison.resumo["shift_motivos_descarte"] = (
            discards.loc[discards["origem"] == "SHIFT", "motivo_descarte"]
            .value_counts()
            .to_dict()
        )
        comparison.resumo["rede_motivos_descarte"] = (
            discards.loc[discards["origem"] == "REDE", "motivo_descarte"]
            .value_counts()
            .to_dict()
        )
        comparison.resumo["motivos_descarte_por_arquivo_rede"] = {
            arquivo: (
                discards.loc[
                    (discards["origem"] == "REDE")
                    & (discards.get("arquivo_origem") == arquivo),
                    "motivo_descarte",
                ].value_counts().to_dict()
            )
            for arquivo in linhas_lidas_por_arquivo
        }
        comparison.descartes = discards
        comparison.auditoria = build_audit_frame(comparison.resumo, discards)
        return EngineOutput(
            comparison=comparison,
            rede_rows=len(raw_rede),
            rede_rows_por_arquivo=rede_rows_por_arquivo,
            shift_rows=len(raw_shift),
            shift_rows_por_arquivo=shift_rows_por_arquivo,
            rede_context=context,
        )

    def export(self, comparison, output_dir: Path) -> None:
        generate_reports(comparison, output_dir)
