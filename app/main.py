from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import pandas as pd
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .application.use_cases.process_reconciliation import (
    ProcessReconciliationCommand,
)
from .application.divergence_ordering import sort_divergences
from .application.models import UploadedSpreadsheet
from .adapters.outbound.spreadsheets.file_reader import read_file_with_metadata
from .adapters.outbound.spreadsheets.normalizer import normalize_authorization, normalize_dataframe
from .auth import SESSION_KEY, authenticate, is_public_path
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

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    # Sem SECRET_KEY definida, gera uma por processo: funciona, mas derruba
    # todas as sessões a cada reinício/deploy. Em produção, defina SECRET_KEY
    # fixa nas variáveis de ambiente do servidor.
    SECRET_KEY = uuid.uuid4().hex
    logging.getLogger("conciliacao").warning(
        "SECRET_KEY não definida no ambiente; usando uma chave temporária "
        "(sessões serão perdidas a cada reinício). Defina SECRET_KEY em produção."
    )
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.middleware("http")
async def exigir_login(request: Request, call_next):
    if is_public_path(request.url.path) or request.session.get(SESSION_KEY):
        response = await call_next(request)
    elif request.method == "GET":
        destino = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
        response = RedirectResponse(f"/login?next={destino}", status_code=303)
    else:
        response = RedirectResponse("/login", status_code=303)
    if not request.url.path.startswith("/static"):
        # Bug real: sem isso, um proxy/CDN no caminho (ou o próprio
        # navegador) pode guardar em cache a resposta autenticada e servi-la
        # depois pra outra pessoa sem sessão válida, pulando o login. Só os
        # arquivos estáticos (CSS etc.) podem ser cacheados com segurança.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


# Precisa ser registrada DEPOIS do middleware @app.middleware("http") acima:
# o Starlette monta a pilha em ordem invertida de app.add_middleware, então
# adicionar o SessionMiddleware por último aqui garante que ele rode por
# fora (antes) do middleware de login, deixando request.session disponível
# quando `exigir_login` é chamado.
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")


def _money(value) -> str:
    number = float(value or 0)
    return f"R$ {number:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")


templates.env.filters["money"] = _money


def _quer_html(request: Request) -> bool:
    """True quando a requisição veio de navegação de navegador (form POST,
    clique em link) e não de uma chamada fetch/JS da própria página.

    As telas de erro "cruas" (JSON puro, ou a página preta de traceback do
    Starlette) só aparecem para navegação normal, porque só nesses casos o
    navegador renderiza a resposta como página. Chamadas fetch() no
    JavaScript da aplicação (resultado.html, verificar_conciliados.html) não
    mandam "text/html" no Accept, então continuam recebendo JSON puro, que é
    o que o JavaScript espera para tratar o erro no próprio lugar."""
    return "text/html" in request.headers.get("accept", "")


_TITULOS_STATUS = {
    400: "Requisição inválida",
    404: "Não encontrado",
    413: "Arquivo muito grande",
    422: "Dados inválidos",
    500: "Erro interno",
}

# Rótulos amigáveis dos campos do formulário de upload, usados para traduzir
# erros de validação do FastAPI (campo ausente/tipo errado no multipart) em
# mensagens que a pessoa usuária entende, em vez do JSON cru do Pydantic.
_ROTULOS_CAMPOS = {
    "arquivo_rede": "Arquivo da Rede Itaú",
    "arquivo_shift": "Arquivo do Shift",
    "unidade_id": "Unidade",
    "data_conciliacao": "Data da conciliação",
    "aba_rede": "Aba da planilha da Rede",
    "aba_shift": "Aba da planilha do Shift",
}


def _pagina_erro(request: Request, status_code: int, mensagem: str, detalhes: list[str] | None = None):
    return templates.TemplateResponse(
        request,
        "erro.html",
        {
            "request": request,
            "status_code": status_code,
            "titulo": _TITULOS_STATUS.get(status_code, "Ocorreu um erro"),
            "mensagem": mensagem,
            "detalhes": detalhes or [],
            "voltar_url": request.headers.get("referer") or "/",
        },
        status_code=status_code,
    )


@app.exception_handler(HTTPException)
async def erro_http(request: Request, exc: HTTPException):
    if _quer_html(request):
        return _pagina_erro(request, exc.status_code, str(exc.detail))
    # Fora de navegação (fetch/JS da própria página), mantém o comportamento
    # padrão do FastAPI: JSON puro, que é o que o JavaScript já trata.
    return JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None)
    )


@app.exception_handler(RequestValidationError)
async def erro_validacao(request: Request, exc: RequestValidationError):
    # 422 do FastAPI antes mesmo da rota rodar: normalmente campo obrigatório
    # ausente ou com tipo errado no multipart/form-data (ex.: unidade_id não
    # numérico). Ver regra do projeto sobre a rota /processar.
    detalhes = []
    for erro in exc.errors():
        campo = str(erro["loc"][-1]) if erro.get("loc") else ""
        rotulo = _ROTULOS_CAMPOS.get(campo, campo or "campo do formulário")
        detalhes.append(f"{rotulo}: {erro.get('msg', 'valor inválido')}")
    if _quer_html(request):
        return _pagina_erro(
            request, 422,
            "Alguns campos do formulário não foram enviados corretamente.",
            detalhes,
        )
    return JSONResponse({"detail": exc.errors()}, status_code=422)


@app.exception_handler(Exception)
async def erro_inesperado(request: Request, exc: Exception):
    # Rede de segurança para exceções que escapam das rotas (ex.: fora dos
    # try/except de /processar). Sem isso, o Starlette mostra uma página de
    # traceback crua (ou 500 em branco fora de modo debug) — sempre logamos
    # o traceback completo e nunca vazamos detalhes internos pra tela.
    logger.exception("Erro não tratado em %s %s", request.method, request.url.path)
    if _quer_html(request):
        return _pagina_erro(
            request, 500,
            "Ocorreu um erro inesperado. Tente novamente; se o problema "
            "continuar, contate o suporte.",
        )
    return JSONResponse({"detail": "Erro interno do servidor."}, status_code=500)


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_form(request: Request, next: str = "/", erro: str = ""):
    return templates.TemplateResponse(request, "login.html", {
        "request": request, "next": next or "/", "erro": erro,
    })


@app.post("/login", include_in_schema=False)
async def login(
    request: Request,
    usuario: str = Form(...),
    senha: str = Form(...),
    next: str = Form("/"),
):
    if not authenticate(usuario.strip(), senha):
        erro = quote("Usuário ou senha inválidos.")
        return RedirectResponse(
            f"/login?erro={erro}&next={quote(next or '/')}",
            status_code=303,
        )
    request.session[SESSION_KEY] = usuario.strip()
    return RedirectResponse(next or "/", status_code=303)


@app.post("/logout", include_in_schema=False)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


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


def _normalizar_autorizacao(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"\.0+$", "", text)
    text = re.sub(r"\s+", "", text)
    return text.upper()


def _mark_key(autorizacao: str, data_vencimento: str, valor: str = "") -> str:
    """Mesmo formato de chave usado em SQLiteRepository._mark_key: combina
    autorização + vencimento + valor, já que a autorização sozinha não
    diferencia parcelas de uma venda parcelada (vencimentos diferentes), e
    autorização+vencimento sozinhos não cobrem o caso raro de valores
    diferentes no mesmo vencimento."""
    return f"{autorizacao}|{data_vencimento or ''}|{valor or ''}"


def _selecionar_unidade_id(unidade_id: int | None) -> int:
    """Resolve a unidade do upload: usa a informada, ou infere quando só há
    uma unidade ativa cadastrada. Lança HTTPException com mensagem clara nos
    demais casos (nenhuma unidade cadastrada, ou mais de uma sem escolha)."""
    if unidade_id is not None:
        return unidade_id
    unidades_ativas = container.units.list(active_only=True)
    if len(unidades_ativas) == 1:
        return int(unidades_ativas[0].id)
    if not unidades_ativas:
        raise HTTPException(
            400,
            "Nenhuma unidade ativa cadastrada. Cadastre uma unidade na área "
            "\"Unidades\" antes de processar os arquivos.",
        )
    raise HTTPException(
        400,
        "Há mais de uma unidade ativa cadastrada; selecione no formulário a "
        "unidade à qual estas planilhas pertencem.",
    )


def _formatar_data_curta(valor) -> str | None:
    """Converte uma data (objeto `date` ou string "AAAA-MM-DD") para
    "DD/MM". Formatos não reconhecidos voltam como vieram (nunca lança
    exceção)."""
    if not valor:
        return None
    if hasattr(valor, "strftime"):
        return valor.strftime("%d/%m")
    partes = str(valor).split("-")
    if len(partes) != 3:
        return str(valor)
    ano, mes, dia = partes
    return f"{dia}/{mes}"


def _chave_ordenacao_data(valor) -> str:
    """Chave de ordenação estável para datas mistas (objeto `date` ou string
    ISO), usada só para achar a menor/maior data de uma janela."""
    if hasattr(valor, "isoformat"):
        return valor.isoformat()
    return str(valor)


def _formatar_janela_datas(datas: list) -> str:
    """Formata um conjunto de datas como janela "DD/MM-DD/MM" (ou uma única
    "DD/MM" quando todas as datas coincidem). Usado para resumir o período
    coberto por várias parcelas da mesma autorização."""
    validas = sorted({d for d in datas if d}, key=_chave_ordenacao_data)
    if not validas:
        return "—"
    inicio, fim = _formatar_data_curta(validas[0]), _formatar_data_curta(validas[-1])
    return inicio if inicio == fim else f"{inicio}-{fim}"


def _vencimento_linha(row: dict) -> str:
    """Vencimento usado para diferenciar parcelas da mesma autorização (a
    autorização se repete entre parcelas de uma venda parcelada; o
    vencimento é o que muda de uma parcela para outra). Prioriza o
    vencimento do Shift — é o lado que o usuário acompanha e onde uma
    parcela pode estar dividida em mais de uma transação."""
    return str(row.get("shift_data_vencimento_normalizado") or row.get("rede_data_vencimento_normalizado") or "")


def _valor_linha(row: dict) -> Decimal:
    """Valor bruto da linha (Shift, com fallback pra Rede quando o Shift não
    tiver o campo). Usado para somar transações do Shift que representem a
    mesma parcela (mesma autorização + mesmo vencimento) e para diferenciar,
    na chave de identificação, parcelas com a mesma autorização e o mesmo
    vencimento mas valores diferentes."""
    for campo in ("valor_bruto_shift", "valor_bruto_rede"):
        valor = row.get(campo)
        if valor is not None and not pd.isna(valor):
            try:
                return Decimal(str(valor))
            except Exception:
                continue
    return Decimal("0")


def _formatar_valor_chave(valor: Decimal) -> str:
    """Representação estável de um valor monetário para uso em chave
    (dict/banco) — quantizado a 2 casas decimais para não depender de como o
    Decimal foi construído (ex.: "150" vs "150.00")."""
    return str(valor.quantize(Decimal("0.01")))


def _agregar_por_autorizacao(conciliados: list[dict]) -> dict[tuple[str, str, str], dict]:
    """Agrupa as linhas conciliadas por (autorização, vencimento Shift, valor).

    Importante:
    - Uma mesma autorização se repete mensalmente entre parcelas de uma
      venda parcelada, cada parcela com seu próprio vencimento — por isso
      vencimentos diferentes NUNCA são combinados (são parcelas distintas).
    - Já a mesma autorização com o MESMO vencimento pode aparecer em mais de
      uma linha do Shift representando uma única parcela da Rede dividida em
      mais de uma transação (ex.: pagamento fracionado) — nesse caso os
      valores são somados, e o valor total somado entra na chave final para
      diferenciar do raro caso de duas parcelas distintas que coincidam em
      autorização e vencimento mas tenham valores diferentes.
    """
    agrupado: dict[tuple[str, str], dict] = {}
    for row in conciliados:
        autorizacao = row.get("shift_autorizacao_normalizado") or row.get("rede_autorizacao_normalizado")
        if not autorizacao:
            continue
        chave_intermediaria = (autorizacao, _vencimento_linha(row))
        item = agrupado.setdefault(chave_intermediaria, {
            "datas_emissao_shift": [], "datas_vencimento_shift": [],
            "datas_venda_rede": [], "datas_vencimento_rede": [],
            "formas_pagamento": set(), "quantidade_linhas": 0,
            "parcela_shift": row.get("parcela_shift"),
            "qtd_parcelas_shift": row.get("qtd_parcelas_shift"),
            "valor_bruto_total": Decimal("0"),
        })
        item["quantidade_linhas"] += 1
        item["datas_emissao_shift"].append(
            row.get("shift_data_emissao_normalizado") or row.get("shift_data_venda_normalizado")
        )
        item["datas_vencimento_shift"].append(row.get("shift_data_vencimento_normalizado"))
        item["datas_venda_rede"].append(row.get("data_venda_rede"))
        item["datas_vencimento_rede"].append(row.get("rede_data_vencimento_normalizado"))
        item["valor_bruto_total"] += _valor_linha(row)
        forma = row.get("modalidade_shift") or row.get("modalidade_rede")
        if forma:
            item["formas_pagamento"].add(str(forma))
    resultado = {}
    for (autorizacao, vencimento), item in agrupado.items():
        valor_str = _formatar_valor_chave(item["valor_bruto_total"])
        resultado[(autorizacao, vencimento, valor_str)] = {
            "janela_emissao_shift": _formatar_janela_datas(item["datas_emissao_shift"]),
            "janela_vencimento_shift": _formatar_janela_datas(item["datas_vencimento_shift"]),
            "janela_venda_rede": _formatar_janela_datas(item["datas_venda_rede"]),
            "janela_vencimento_rede": _formatar_janela_datas(item["datas_vencimento_rede"]),
            "forma_pagamento": ", ".join(sorted(item["formas_pagamento"])) or "—",
            "quantidade_linhas": item["quantidade_linhas"],
            "parcela_shift": item["parcela_shift"],
            "qtd_parcelas_shift": item["qtd_parcelas_shift"],
            "valor_bruto_total": item["valor_bruto_total"],
        }
    return resultado


def _montar_contexto_resultado(
    data: dict, conciliacao, arquivo_rede: str, page_param: str,
) -> dict:
    """Monta todo o contexto usado pelo template `resultado.html`: filtragem
    de divergências/conciliados por status e por arquivo de origem,
    paginação, e agregação de autorizações conciliadas. Extraído da rota
    `/resultado/{result_id}` para manter o handler HTTP enxuto."""
    autorizacoes_marcadas = conciliacao.autorizacoes_marcadas if conciliacao else {}
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
    try:
        page = max(int(page_param or "1"), 1)
    except ValueError:
        page = 1
    page_size = 50
    total_divergencias = len(divergencias)
    total_paginas = max((total_divergencias + page_size - 1) // page_size, 1)
    page = min(page, total_paginas)
    start = (page - 1) * page_size
    end = start + page_size
    divergencias_paginadas = divergencias[start:end]
    row_shift_por_autorizacao = _agregar_por_autorizacao(conciliados)
    autorizacoes_conciliadas = sorted(row_shift_por_autorizacao.keys())
    quantidade_autorizacoes_conciliadas = len(autorizacoes_conciliadas)
    total_autorizacoes_marcadas = sum(
        1 for chave in autorizacoes_conciliadas
        if autorizacoes_marcadas.get(_mark_key(*chave))
    )
    total_autorizacoes_pendentes = quantidade_autorizacoes_conciliadas - total_autorizacoes_marcadas
    return {
        "resumo": data["resumo"],
        "divergencias": divergencias_paginadas,
        "total_divergencias": total_divergencias,
        "pagina_atual": page,
        "total_paginas": total_paginas,
        "qualidade": data["qualidade_shift"][:200],
        "auditoria": data.get("auditoria", []),
        "descartes": data.get("descartes", [])[:200],
        "conciliacao": conciliacao,
        "arquivos_rede": arquivos_rede,
        "arquivo_rede_selecionado": arquivo_rede,
        "autorizacoes_conciliadas": autorizacoes_conciliadas,
        "row_shift_por_autorizacao": row_shift_por_autorizacao,
        "autorizacoes_marcadas": autorizacoes_marcadas,
        "quantidade_autorizacoes_conciliadas": quantidade_autorizacoes_conciliadas,
        "total_autorizacoes_marcadas": total_autorizacoes_marcadas,
        "total_autorizacoes_pendentes": total_autorizacoes_pendentes,
    }


def _construir_linhas_verificacao(
    rede: pd.DataFrame, parcelas_conciliadas: set[tuple[str, str]],
) -> list[dict]:
    """Monta as linhas exibidas em `/verificar-conciliados`: uma por
    autorização distinta encontrada nos arquivos enviados, com o status
    "JÁ CONCILIADA"/"PARCIALMENTE CONCILIADA"/"NÃO CONCILIADA" conforme o
    banco de histórico.

    Importante: a mesma autorização pode aparecer em mais de uma linha do
    arquivo da Rede — parcelas diferentes (vencimentos diferentes) de uma
    mesma venda parcelada. Bug real corrigido aqui: antes, a autorização
    inteira virava "JÁ CONCILIADA" assim que QUALQUER UMA de suas parcelas
    fosse marcada em `/resultado/{id}` (que agora marca por parcela, não por
    autorização inteira) — o que fazia um operador achar que uma parcela
    ainda pendente já tinha sido conciliada. Por isso o agrupamento aqui
    também é feito por (autorização, vencimento), e o status reflete quantas
    dessas parcelas já foram marcadas: todas -> "JÁ CONCILIADA", algumas ->
    "PARCIALMENTE CONCILIADA (x/y parcelas)", nenhuma -> "NÃO CONCILIADA".
    """
    agrupado: dict[str, dict] = {}
    for _, row in rede.iterrows():
        autorizacao = _normalizar_autorizacao(row.get("autorizacao"))
        if not autorizacao:
            continue
        item = agrupado.setdefault(autorizacao, {
            "arquivos": set(), "datas_venda": [], "datas_vencimento": [],
            "modalidades": set(), "bandeiras": set(),
            "valor_bruto_total": Decimal("0"), "valor_liquido_total": Decimal("0"),
            "quantidade_linhas": 0,
            "vencimentos": set(),
        })
        item["quantidade_linhas"] += 1
        if row.get("_arquivo_verificacao"):
            item["arquivos"].add(str(row.get("_arquivo_verificacao")))
        item["datas_venda"].append(row.get("data_venda"))
        item["datas_vencimento"].append(row.get("data_vencimento"))
        item["vencimentos"].add(str(row.get("data_vencimento") or ""))
        if row.get("modalidade"):
            item["modalidades"].add(str(row.get("modalidade")))
        if row.get("bandeira"):
            item["bandeiras"].add(str(row.get("bandeira")))
        for campo, chave in (("valor_bruto", "valor_bruto_total"), ("valor_liquido", "valor_liquido_total")):
            valor = row.get(campo)
            if valor is not None and not pd.isna(valor):
                item[chave] += Decimal(str(valor))

    linhas = []
    for autorizacao, item in agrupado.items():
        total_parcelas = len(item["vencimentos"])
        parcelas_marcadas = sum(
            1 for vencimento in item["vencimentos"]
            if (autorizacao, vencimento) in parcelas_conciliadas
        )
        if parcelas_marcadas == 0:
            conciliada, status = False, "NÃO CONCILIADA"
        elif parcelas_marcadas == total_parcelas:
            conciliada, status = True, "JÁ CONCILIADA"
        else:
            conciliada, status = False, f"PARCIALMENTE CONCILIADA ({parcelas_marcadas}/{total_parcelas} parcelas)"
        linhas.append({
            "autorizacao": autorizacao,
            "conciliada": conciliada,
            "status": status,
            "arquivo": " | ".join(sorted(item["arquivos"])) or None,
            "janela_data_venda": _formatar_janela_datas(item["datas_venda"]),
            "janela_data_vencimento": _formatar_janela_datas(item["datas_vencimento"]),
            "quantidade_linhas": item["quantidade_linhas"],
            "valor_bruto": item["valor_bruto_total"],
            "valor_liquido": item["valor_liquido_total"],
            "bandeira": ", ".join(sorted(item["bandeiras"])) or None,
            "modalidade": ", ".join(sorted(item["modalidades"])) or None,
        })
    return linhas


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
    unidade_id = _selecionar_unidade_id(unidade_id)
    unidade = container.units.get(unidade_id)
    if not unidade or not unidade.ativa:
        raise HTTPException(400, "A unidade selecionada não existe ou está inativa. Escolha uma unidade ativa.")
    try:
        reconciliation_date = date.fromisoformat(data_conciliacao).isoformat()
    except ValueError as exc:
        raise HTTPException(
            400,
            f'Data de conciliação inválida: "{data_conciliacao}". Use o formato AAAA-MM-DD.',
        ) from exc
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
        raise HTTPException(404, "Resultado não encontrado. Verifique o link ou refaça a conciliação.")
    data = json.loads(path.read_text(encoding="utf-8"))
    conciliacao = container.history.get(result_id)
    contexto = _montar_contexto_resultado(
        data, conciliacao, arquivo_rede, request.query_params.get("page", "1")
    )
    return templates.TemplateResponse(request, "resultado.html", {
        "request": request, "id": result_id, **contexto,
    })


@app.post("/resultado/{result_id}/autorizacoes")
async def marcar_autorizacao_conciliada(result_id: str, request: Request):
    _result_path(result_id)
    body = await request.json()
    autorizacao = str(body.get("autorizacao") or "").strip()
    if not autorizacao:
        raise HTTPException(400, "Informe um número de autorização para marcar como conciliada.")
    conciliado = bool(body.get("conciliado", True))
    # data_vencimento + valor diferenciam parcelas da mesma autorização (uma
    # venda parcelada repete a autorização mensalmente, cada parcela com seu
    # próprio vencimento; e, mais raramente, o mesmo vencimento pode ter
    # valores diferentes). Sem isso, marcar uma parcela marcaria a
    # autorização inteira, "conciliando" outras parcelas ainda pendentes.
    data_vencimento = str(body.get("data_vencimento") or "").strip()
    valor = str(body.get("valor") or "").strip()
    container.history.set_authorization_mark(result_id, autorizacao, conciliado, data_vencimento, valor)
    return JSONResponse({
        "ok": True, "autorizacao": autorizacao, "conciliado": conciliado,
        "data_vencimento": data_vencimento, "valor": valor,
    })


@app.get("/verificar-conciliados/busca")
async def buscar_autorizacao_conciliada(autorizacao: str, unidade_id: str = ""):
    """Busca rápida (sem precisar enviar arquivo): diz se um número de
    autorização já está marcado como conciliado no banco. Não depende de
    nenhum upload — consulta direto à tabela de marcações.

    Importante: a marcação é por parcela (autorização + vencimento), então
    uma autorização com mais de uma parcela pode estar parcialmente
    conciliada. Como esta busca não recebe vencimento (só o número), ela não
    afirma "JÁ CONCILIADA" de forma binária — reporta quantas parcelas
    distintas dessa autorização já foram marcadas, para não sugerir
    incorretamente que tudo já foi conciliado quando só uma parcela foi.
    """
    try:
        normalizada = normalize_authorization(autorizacao)
    except ValueError:
        raise HTTPException(400, "Número de autorização inválido (máximo de 6 dígitos).")
    if not normalizada:
        raise HTTPException(400, "Informe um número de autorização para buscar.")
    unit_id = int(unidade_id) if unidade_id.strip() else None
    parcelas_banco = container.history.list_conciliated_installments(unit_id)
    parcelas_marcadas = sorted({
        vencimento for auth, vencimento in parcelas_banco if auth == normalizada
    })
    conciliada = len(parcelas_marcadas) > 0
    if not conciliada:
        status = "NÃO CONCILIADA"
    elif len(parcelas_marcadas) == 1:
        status = f"1 parcela conciliada (vencimento {parcelas_marcadas[0] or 'sem data'})"
    else:
        status = f"{len(parcelas_marcadas)} parcelas conciliadas (vencimentos: {', '.join(v or 'sem data' for v in parcelas_marcadas)})"
    return JSONResponse({
        "autorizacao": normalizada,
        "conciliada": conciliada,
        "status": status,
    })


@app.get("/verificar-conciliados", response_class=HTMLResponse)
async def verificar_conciliados_form(request: Request):
    return templates.TemplateResponse(request, "verificar_conciliados.html", {
        "request": request,
        "resultado": None,
        "unidades": container.units.list(),
        "unidade_id_selecionada": None,
    })


@app.post("/verificar-conciliados", response_class=HTMLResponse)
async def verificar_conciliados(
    request: Request,
    arquivo_rede: list[UploadFile] = File(...),
    aba_rede: str = Form(""),
    unidade_id: str = Form(""),
):
    unidade_id = int(unidade_id) if unidade_id.strip() else None
    upload_dir = UPLOAD_DIR / "verificar_conciliados"
    upload_dir.mkdir(parents=True, exist_ok=True)
    try:
        try:
            frames = []
            arquivos_nomes = []
            total_linhas = 0
            for index, upload in enumerate(arquivo_rede, start=1):
                path = await _save_upload(upload, upload_dir, f"rede_verificacao_{index:02d}")
                raw_rede, metadata = read_file_with_metadata(path, aba_rede.strip() or None)
                rede = normalize_dataframe(raw_rede, "rede")
                rede["_arquivo_verificacao"] = upload.filename or path.name
                rede["_aba_verificacao"] = metadata.get("sheet_name")
                frames.append(rede)
                arquivos_nomes.append(upload.filename or path.name)
                total_linhas += len(rede)
        except HTTPException:
            raise
        except (ValueError, DomainError) as exc:
            # Mesmo padrão de /processar: erros de leitura/normalização já
            # trazem mensagem amigável (ex.: "aba inexistente", "arquivo
            # vazio"); logamos para investigar casos inesperados.
            logger.exception("Erro ao ler arquivo em /verificar-conciliados")
            raise HTTPException(422, str(exc)) from exc
        except Exception:
            logger.exception("Erro inesperado em /verificar-conciliados")
            raise HTTPException(
                500,
                "Ocorreu um erro inesperado ao ler os arquivos enviados. "
                "Confira se o arquivo não está corrompido e tente novamente.",
            )

        rede = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        parcelas_conciliadas = container.history.list_conciliated_installments(unidade_id)
        linhas = _construir_linhas_verificacao(rede, parcelas_conciliadas)
        unidade_selecionada = container.units.get(unidade_id) if unidade_id else None
        resultado = {
            "arquivo": " | ".join(arquivos_nomes),
            "sheet": aba_rede.strip() or "Automática",
            "unidade": f"{unidade_selecionada.codigo} — {unidade_selecionada.nome}" if unidade_selecionada else "Todas as unidades",
            "total_linhas": total_linhas,
            "total_autorizacoes": len(linhas),
            "total_conciliadas": sum(1 for linha in linhas if linha["conciliada"]),
            "total_nao_conciliadas": sum(1 for linha in linhas if not linha["conciliada"]),
            "linhas": linhas,
            "modalidades": sorted({
                modalidade.strip()
                for linha in linhas if linha["modalidade"]
                for modalidade in linha["modalidade"].split(",")
            }),
        }
        return templates.TemplateResponse(request, "verificar_conciliados.html", {
            "request": request,
            "resultado": resultado,
            "unidades": container.units.list(),
            "unidade_id_selecionada": unidade_id,
        })
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)


@app.get("/download/{result_id}/{report_name}")
async def download(result_id: str, report_name: str):
    names = {
        "detalhado": "relatorio_detalhado.xlsx",
        "resumo": "resumo.xlsx",
        "qualidade-shift": "qualidade_shift.xlsx",
    }
    if report_name not in names:
        raise HTTPException(
            404,
            f'Relatório "{report_name}" não existe. Opções válidas: '
            f'{", ".join(sorted(names))}.',
        )
    path = _result_path(result_id) / names[report_name]
    if not path.exists():
        raise HTTPException(
            404,
            "Este relatório ainda não foi gerado ou o resultado não existe mais. "
            "Refaça a conciliação se necessário.",
        )
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
