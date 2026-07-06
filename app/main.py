from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .application.use_cases.process_reconciliation import (
    ProcessReconciliationCommand,
)
from .application.divergence_ordering import sort_divergences
from .application.models import UploadedSpreadsheet
from .bootstrap.container import container
from .domain.exceptions import DomainError


BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
RESULT_DIR = STORAGE_DIR / "results"
ALLOWED_SUFFIXES = {".csv", ".xls", ".xlsx"}
MAX_FILE_SIZE = 50 * 1024 * 1024
for directory in (UPLOAD_DIR, RESULT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("conciliacao")

app = FastAPI(title="Conciliação Rede Itaú x Shift", version="0.1.0")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _money(value) -> str:
    number = float(value or 0)
    return f"R$ {number:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")


templates.env.filters["money"] = _money


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


def _result_path(result_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", result_id):
        raise HTTPException(404, "Resultado não encontrado.")
    return RESULT_DIR / result_id


async def _save_upload(upload: UploadFile, directory: Path, prefix: str) -> Path:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(400, f"{prefix}: formato inválido. Use CSV, XLS ou XLSX.")
    path = directory / f"{prefix}{suffix}"
    size = 0
    with path.open("wb") as target:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_SIZE:
                target.close()
                path.unlink(missing_ok=True)
                raise HTTPException(413, f"{prefix}: arquivo excede 50 MB.")
            target.write(chunk)
    return path


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "request": request,
        "default_date": (date.today() - timedelta(days=1)).isoformat(),
        "unidades": container.units.list(active_only=True),
    })


@app.post("/processar")
async def processar(
    arquivo_rede: list[UploadFile] = File(...),
    arquivo_shift: list[UploadFile] = File(...),
    aba_rede: str = Form(""),
    aba_shift: str = Form(""),
    data_conciliacao: str = Form(""),
    unidade_id: int | None = Form(None),
):
    if unidade_id is None:
        unidades_ativas = container.units.list(active_only=True)
        if len(unidades_ativas) == 1:
            unidade_id = int(unidades_ativas[0].id)
        elif not unidades_ativas:
            raise HTTPException(
                400,
                "Cadastre uma unidade na área Unidades antes de processar os arquivos.",
            )
        else:
            raise HTTPException(
                400,
                "Selecione a unidade à qual estas planilhas pertencem.",
            )
    unidade = container.units.get(unidade_id)
    if not unidade or not unidade.ativa:
        raise HTTPException(400, "Selecione uma unidade ativa.")
    try:
        reconciliation_date = date.fromisoformat(data_conciliacao).isoformat()
    except ValueError as exc:
        raise HTTPException(400, "Informe uma data de conciliação válida.") from exc
    result_id = uuid.uuid4().hex
    upload_dir = UPLOAD_DIR / result_id
    output_dir = RESULT_DIR / result_id
    upload_dir.mkdir(parents=True)
    try:
        rede_files: list[UploadedSpreadsheet] = []
        for index, rede_upload in enumerate(arquivo_rede, start=1):
            rede_path = await _save_upload(rede_upload, upload_dir, f"rede_{index:02d}")
            rede_files.append(UploadedSpreadsheet(
                origem=f"REDE_{index:02d}",
                categoria="REDE",
                path=rede_path,
                nome_original=rede_upload.filename or rede_path.name,
                sheet=aba_rede.strip() or None,
            ))
        shift_files: list[UploadedSpreadsheet] = []
        for index, shift_upload in enumerate(arquivo_shift, start=1):
            shift_path = await _save_upload(shift_upload, upload_dir, f"shift_{index:02d}")
            shift_files.append(UploadedSpreadsheet(
                origem=f"SHIFT_{index:02d}",
                categoria="SHIFT",
                path=shift_path,
                nome_original=shift_upload.filename or shift_path.name,
                sheet=aba_shift.strip() or None,
            ))
        container.process_reconciliation.execute(ProcessReconciliationCommand(
            result_id=result_id,
            unit_id=unidade_id,
            reconciliation_date=reconciliation_date,
            rede_files=tuple(rede_files),
            shift_files=tuple(shift_files),
            output_dir=output_dir,
        ))
    except HTTPException:
        shutil.rmtree(upload_dir, ignore_errors=True)
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    except (ValueError, DomainError) as exc:
        # Erros esperados de leitura/normalização/comparação já trazem
        # mensagem amigável (ex.: "arquivo vazio", "aba inexistente"). Mas
        # também podem ser ValueError "crus" vindos de bibliotecas (ex.:
        # "cannot convert float NaN to integer"), então logamos o traceback
        # completo para conseguir localizar a causa real depois.
        logger.exception(
            "ValueError/DomainError ao processar conciliação result_id=%s", result_id
        )
        shutil.rmtree(upload_dir, ignore_errors=True)
        shutil.rmtree(output_dir, ignore_errors=True)
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        # Erro inesperado: registra o traceback completo no log do servidor
        # e mostra uma mensagem genérica ao usuário, sem vazar detalhes internos.
        shutil.rmtree(upload_dir, ignore_errors=True)
        shutil.rmtree(output_dir, ignore_errors=True)
        logger.exception("Erro inesperado ao processar conciliação result_id=%s", result_id)
        raise HTTPException(
            500,
            "Ocorreu um erro inesperado ao processar os arquivos. "
            "Tente novamente; se o problema continuar, contate o suporte.",
        )
    return RedirectResponse(f"/resultado/{result_id}", status_code=303)


@app.get("/resultado/{result_id}", response_class=HTMLResponse)
async def resultado(request: Request, result_id: str, arquivo_rede: str = ""):
    path = _result_path(result_id) / "resultado.json"
    if not path.exists():
        raise HTTPException(404, "Resultado não encontrado.")
    data = json.loads(path.read_text(encoding="utf-8"))
    conciliacao = container.history.get(result_id)
    status_conciliado = {
        "CONCILIADO",
        "CONCILIADO_POR_AGRUPAMENTO_SHIFT",
        "CONCILIADO_POR_AGRUPAMENTO_OS_MESMA_AUTORIZACAO",
        "CONCILIADO_COM_PARCELA_COMPATIVEL_POR_RESTANTES",
    }
    divergencias = [
        row for row in data["detalhado"]
        if row["status_comparacao"] not in status_conciliado
    ]
    conciliados = [
        row for row in data["detalhado"]
        if row["status_comparacao"] in status_conciliado
    ]
    arquivos_rede = sorted({
        row.get("rede_arquivo_origem")
        for row in data["detalhado"]
        if row.get("rede_arquivo_origem")
    })
    if arquivo_rede:
        divergencias = [
            row for row in divergencias
            if row.get("rede_arquivo_origem") == arquivo_rede
        ]
        conciliados = [
            row for row in conciliados
            if row.get("rede_arquivo_origem") == arquivo_rede
        ]
    divergencias = sort_divergences(divergencias)
    autorizacoes_conciliadas = sorted({
        (row.get("shift_autorizacao_normalizado") or row.get("rede_autorizacao_normalizado"))
        for row in conciliados
        if row.get("shift_autorizacao_normalizado") or row.get("rede_autorizacao_normalizado")
    })
    return templates.TemplateResponse(request, "resultado.html", {
        "request": request, "id": result_id, "resumo": data["resumo"],
        "divergencias": divergencias[:200],
        "qualidade": data["qualidade_shift"][:200],
        "auditoria": data.get("auditoria", []),
        "descartes": data.get("descartes", [])[:200],
        "conciliacao": conciliacao,
        "arquivos_rede": arquivos_rede,
        "arquivo_rede_selecionado": arquivo_rede,
        "autorizacoes_conciliadas": autorizacoes_conciliadas,
    })


@app.get("/download/{result_id}/{report_name}")
async def download(result_id: str, report_name: str):
    names = {
        "detalhado": "relatorio_detalhado.xlsx",
        "resumo": "resumo.xlsx",
        "qualidade-shift": "qualidade_shift.xlsx",
    }
    if report_name not in names:
        raise HTTPException(404, "Relatório inválido.")
    path = _result_path(result_id) / names[report_name]
    if not path.exists():
        raise HTTPException(404, "Resultado não encontrado.")
    return FileResponse(
        path,
        filename=f"{result_id[:8]}_{names[report_name]}",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/unidades", response_class=HTMLResponse)
async def unidades(request: Request, erro: str = ""):
    return templates.TemplateResponse(request, "unidades.html", {
        "request": request, "unidades": container.units.list(), "erro": erro,
    })


@app.post("/unidades")
async def adicionar_unidade(
    codigo: str = Form(...),
    nome: str = Form(...),
    estabelecimento: str = Form(""),
    empresa_shift: str = Form(""),
):
    try:
        container.units.create(codigo, nome, estabelecimento, empresa_shift)
    except DomainError as exc:
        return RedirectResponse(f"/unidades?erro={quote(str(exc))}", status_code=303)
    return RedirectResponse("/unidades", status_code=303)


@app.get("/unidades/{unit_id}/editar", response_class=HTMLResponse)
async def editar_unidade_form(request: Request, unit_id: int, erro: str = ""):
    unidade = container.units.get(unit_id)
    if not unidade:
        raise HTTPException(404, "Unidade não encontrada.")
    return templates.TemplateResponse(request, "editar_unidade.html", {
        "request": request, "unidade": unidade, "erro": erro,
    })


@app.post("/unidades/{unit_id}/editar")
async def editar_unidade(
    unit_id: int,
    codigo: str = Form(...),
    nome: str = Form(...),
    estabelecimento: str = Form(""),
    ativa: str = Form(""),
    empresa_shift: str = Form(""),
):
    try:
        container.units.update(
            unit_id, codigo, nome, estabelecimento, ativa == "on", empresa_shift
        )
    except DomainError as exc:
        return RedirectResponse(
            f"/unidades/{unit_id}/editar?erro={quote(str(exc))}", status_code=303
        )
    return RedirectResponse("/unidades", status_code=303)


@app.post("/unidades/{unit_id}/excluir")
async def excluir_unidade(unit_id: int):
    try:
        container.units.delete(unit_id)
    except DomainError as exc:
        return RedirectResponse(f"/unidades?erro={quote(str(exc))}", status_code=303)
    return RedirectResponse("/unidades", status_code=303)


@app.get("/historico", response_class=HTMLResponse)
async def historico(
    request: Request,
    unidade_id: int | None = None,
    data_inicial: str = "",
    data_final: str = "",
):
    return templates.TemplateResponse(request, "historico.html", {
        "request": request,
        "unidades": container.units.list(),
        "conciliacoes": container.history.list(
            unidade_id, data_inicial or None, data_final or None
        ),
        "filtros": {
            "unidade_id": unidade_id,
            "data_inicial": data_inicial,
            "data_final": data_final,
        },
    })


@app.get("/arquivo/{result_id}/{origem}")
async def baixar_arquivo_original(result_id: str, origem: str):
    _result_path(result_id)
    conciliacao = container.history.get(result_id)
    origem = origem.upper()
    if not conciliacao or origem not in conciliacao.arquivos:
        raise HTTPException(404, "Arquivo original não encontrado.")
    arquivo = conciliacao.arquivos[origem]
    path = Path(arquivo.caminho_arquivo)
    if not path.exists():
        raise HTTPException(404, "Arquivo original não está mais no armazenamento.")
    return FileResponse(path, filename=arquivo.nome_original)


@app.post("/historico/{result_id}/excluir")
async def excluir_conciliacao(result_id: str):
    result_dir = _result_path(result_id)
    try:
        file_paths = container.history.delete(result_id)
    except DomainError as exc:
        raise HTTPException(404, str(exc)) from exc
    for file_path in file_paths:
        path = Path(file_path)
        if path.is_file() and path.parent == (UPLOAD_DIR / result_id).resolve():
            path.unlink(missing_ok=True)
    shutil.rmtree(UPLOAD_DIR / result_id, ignore_errors=True)
    shutil.rmtree(result_dir, ignore_errors=True)
    return RedirectResponse("/historico", status_code=303)
