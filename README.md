# Conciliação Rede Itaú × Shift

MVP web para conciliação operacional D-1 por upload de arquivos CSV, XLS ou XLSX.

Agora a aplicação também possui cadastro de unidades e histórico persistente por
unidade e data. O banco SQLite é criado automaticamente em
`app/storage/conciliacao.db`.

O backend segue Clean Architecture com Ports and Adapters. A documentação do
desenho e do fluxo de dependências está em `docs/ARCHITECTURE.md`.

## O que a aplicação faz

- detecta separador e encoding de CSV;
- identifica automaticamente a aba `pagamentos` e a linha real de cabeçalho;
- remove colunas extras totalmente vazias;
- mapeia pequenas variações dos nomes das colunas;
- preserva os valores originais para auditoria;
- normaliza autorização, NSU, valores, datas, bandeira, modalidade e parcelas;
- valida a qualidade interna do Shift antes da conciliação;
- compara por chave forte e por chaves alternativas;
- mostra resumo e divergências na tela;
- exporta resumo, detalhado e qualidade do Shift em XLSX.
- permite cadastrar, editar, ativar, desativar e excluir unidades sem histórico;
- registra cada conciliação por unidade e dia;
- permite filtrar o histórico por unidade e intervalo de datas;
- conserva e permite baixar novamente os arquivos originais;
- permite excluir uma conciliação e seus arquivos mediante confirmação.

## Instalação

Requer Python 3.11 ou superior.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8001
```

Acesse `http://localhost:8001`.

Também é possível iniciar com:

```powershell
python run.py
```

## Executar com Docker Compose

Com Docker Desktop aberto, execute na pasta do projeto:

```powershell
docker compose up --build -d
```

A aplicação ficará disponível em:

```text
http://localhost:8001
```

Para acompanhar os registros:

```powershell
docker compose logs -f conciliacao
```

Para parar a aplicação sem apagar os dados:

```powershell
docker compose down
```

O banco SQLite, os arquivos enviados e os relatórios ficam no volume Docker
`conciliacao_rede_shift_data`. Recriar a imagem ou o contêiner não apaga esse
volume.

Para apagar também todo o banco e os arquivos armazenados:

```powershell
docker compose down -v
```

Esse último comando é destrutivo e deve ser usado apenas quando você realmente
quiser reiniciar a aplicação sem histórico.

## Exemplo de uso

1. Em **Arquivo Rede Itaú**, selecione o Excel da Rede.
2. Antes do primeiro uso, abra **Unidades** e cadastre ao menos uma unidade.
3. Em **Nova conciliação**, escolha a unidade e o dia das planilhas.
4. Em **Arquivo Shift**, selecione o CSV ou Excel exportado do Shift.
5. Deixe a aba vazia para detecção automática ou informe o nome exato.
6. Clique em **Processar conciliação**.
7. Consulte o resumo e baixe os três relatórios.
8. Use **Histórico** para filtrar por unidade e período.

Para os anexos usados no desenvolvimento, `Pasta1.xlsx` é tratado como Rede e
`208485.csv` como Shift. O Excel possui a aba `pagamentos` e uma linha de título
antes do cabeçalho. O CSV usa `;` e possui milhares de colunas extras vazias;
ambos os casos são tratados automaticamente.

O formato completo `Rede_Rel_Recebimentos_*.xlsx` também é suportado. Ele pode
conter abas como `capa`, `pagamentos`, `ajustes`, `pagamentos futuros`,
`cancelamentos e contestações`, `bloqueados` e `recebidos`. Para a conciliação
transacional, a aplicação seleciona automaticamente a aba `pagamentos`.

Antes de comparar, o sistema confere a data de recebimento e, quando a unidade
possui um número de estabelecimento cadastrado, verifica se esse número aparece
no arquivo da Rede. Isso evita gravar uma planilha no dia ou na unidade errados.

## Normalização

### Autorização

A autorização nunca é convertida para inteiro. Espaços e o sufixo `.0` são
removidos, o texto é convertido para maiúsculas e completado à esquerda até seis
posições. Assim, `55040` e `055040` resultam em `055040`. Identificadores
alfanuméricos, como `M02305`, são preservados. Mais de seis posições gera
`AUTORIZACAO_TAMANHO_INVALIDO`. O valor original continua no relatório.

### Outros campos

- NSU e demais identificadores permanecem texto;
- valores brasileiros são convertidos para `Decimal` com duas casas;
- datas são convertidas para data, aceitando formatos brasileiros e ISO;
- bandeira e modalidade são classificadas em valores padronizados;
- `1`, `01`, `1/3` e `3x` são interpretados como parcelas quando aplicável;
- textos são comparados sem diferença de acentos, caixa ou espaços extras.

## Correspondência e classificações

A busca começa pela chave forte:

`autorização + NSU + valor bruto + parcela + número de parcelas + estabelecimento`

Se não houver resultado, são usadas chaves alternativas para localizar erro de
autorização, NSU, valor ou uma correspondência próxima. Cada linha da Rede só
pode ser consumida uma vez. Uma transação pode receber vários status, separados
por ` + `.

- `CONCILIADO`: todos os dados críticos comparáveis conferem;
- `ERRO_CADASTRAL_SHIFT`: falha encontrada antes da comparação;
- `NAO_ENCONTRADO_NA_REDE` / `NAO_ENCONTRADO_NO_SHIFT`: existe apenas de um lado;
- `DIVERGENCIA_*`: o campo indicado difere entre os lados;
- `POSSIVEL_DUPLICIDADE`: repetição interna do Shift;
- `POSSIVEL_CORRESPONDENCIA`: chave aproximada encontrada;
- `CANCELAMENTO_CONTESTACAO`: transação exige tratamento operacional;
- `REVISAO_MANUAL`: diferença sem correção automática segura.

O MVP apenas aponta divergências. Não altera o Shift, não usa API da Rede e não
executa RPA.

## Método aplicado aos relatórios Rede × Shift

Para relatórios financeiros multiempresa do Shift, a conciliação segue estas
etapas:

1. mantém apenas cartões e ignora PIX/outras formas;
2. filtra a coluna `Empresa` pelo valor cadastrado na unidade; a coluna
   `Descrição Credor/Devedor` é apenas a contraparte e não identifica a unidade;
3. normaliza identificadores, valores, parcelas, datas e bandeiras
   (`MASTER` e `MASTERCARD`, por exemplo, tornam-se `MASTERCARD`);
4. consolida várias linhas do Shift quando possuem a mesma autorização e
   parcela e sua soma corresponde inequivocamente a uma única linha da Rede;
5. compara primeiro por chaves fortes e, sem NSU no Shift, usa
   `autorização + parcela + número de parcelas` para localizar diferenças de
   centavos sem classificá-las incorretamente como transações ausentes.

Campos com significados diferentes entre os sistemas, como lote de caixa e
status contábil do Shift versus lote/status de liquidação da Rede, continuam
preservados para auditoria, mas não geram falsa divergência.

Diferenças monetárias de até `R$ 0,02` são aceitas na conciliação. Elas recebem
o status `CONCILIADO_COM_DIVERGENCIA_TOLERADA`, contam como conciliadas e
permanecem visíveis no painel de divergências e no relatório detalhado, com os
valores das diferenças bruta e líquida.

O relatório detalhado possui três abas:

- `Detalhado`: toda transação válida conciliada, divergente ou não encontrada;
- `Auditoria`: contagens de cada etapa do pipeline;
- `Descartes`: linhas ignoradas com origem, número original e motivo explícito.

Agrupamentos Shift preservam as linhas, códigos, valores e descrições originais
e recebem `CONCILIADO_POR_AGRUPAMENTO_SHIFT`. Conflitos em unidade, data,
modalidade, bandeira ou parcelas recebem `AGRUPAMENTO_SHIFT_AMBIGUO`.

No relatório financeiro do Shift, `Data de emissão` é comparada com a data da
venda da Rede. Já `Vencimento` é preservado para auditoria, mas não gera
divergência contra o vencimento da Rede: uma faixa de vencimentos do Shift pode
ser liquidada pela adquirente em um único dia, especialmente na virada de dias
não úteis. Todas as linhas da faixa exportada continuam na conciliação.

## Testes

```powershell
pytest
```

Os testes cobrem zero à esquerda, autorização inválida e alfanumérica, valores
brasileiros, CSV sujo, validação do Shift, conciliação, divergências, ausências e
duplicidade.

## Estrutura

```text
app/
  domain/
    entities.py
    exceptions.py
  application/
    ports/
    use_cases/
  adapters/
    outbound/
      sqlite_repository.py
      pandas_reconciliation.py
      spreadsheets/
  bootstrap/
    container.py
  main.py
  templates/
  static/
  storage/
tests/
requirements.txt
run.py
```

Os uploads e resultados ficam em `app/storage/`. Em produção, configure
retenção, autenticação, criptografia, trilha de auditoria e descarte seguro,
pois os arquivos contêm dados financeiros.

## Banco e administração

O banco possui três grupos principais:

- `unidades`: cadastro administrável das unidades;
- `conciliacoes`: resultado, unidade e data de cada processamento;
- `importacoes`: arquivos Rede e Shift associados a cada conciliação.

O arquivo original não é colocado como um bloco binário dentro do SQLite. Ele
fica em `app/storage/uploads/<id>/`, enquanto o banco guarda seu nome, origem,
caminho e quantidade de linhas. Essa separação mantém os filtros rápidos e
preserva o arquivo para auditoria.

Unidades com histórico não podem ser apagadas diretamente. Elas podem ser
desativadas, deixando de aparecer em novos uploads, sem quebrar resultados
antigos. Se for realmente necessário removê-las, primeiro exclua suas
conciliações na tela **Histórico**.
