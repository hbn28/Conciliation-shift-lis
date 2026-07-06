# Arquitetura do sistema

O backend usa Clean Architecture com Ports and Adapters (Arquitetura
Hexagonal). A regra de dependência é sempre de fora para dentro:

```text
Adaptadores / FastAPI
          ↓
Aplicação / casos de uso / portas
          ↓
Domínio
```

O domínio não importa FastAPI, SQLite, pandas, Excel, HTML ou Docker.

## Camadas

### `app/domain`

Núcleo independente:

- `entities.py`: `Unit`, `Reconciliation`, `ImportedFile` e dados de gravação;
- `exceptions.py`: erros de domínio, conflito e entidade não encontrada.

### `app/application`

Orquestra o comportamento do sistema:

- `ports/repositories.py`: contratos dos repositórios;
- `ports/reconciliation.py`: contrato do motor de conciliação;
- `use_cases/units.py`: gestão de unidades;
- `use_cases/history.py`: consulta e exclusão do histórico;
- `use_cases/process_reconciliation.py`: processamento completo dos arquivos.

Os casos de uso recebem portas por injeção de dependência. Eles não criam
conexões SQLite e não leem Excel diretamente.

### `app/adapters/outbound`

Implementações técnicas substituíveis:

- `sqlite_repository.py`: implementa as portas de persistência;
- `pandas_reconciliation.py`: implementa a porta do motor;
- `spreadsheets/`: leitura, normalização, auditoria, comparação e exportação tabular.

`spreadsheets/audit.py` mantém a contabilidade das linhas válidas e descartadas.
O adaptador pandas agrega essas informações ao `ComparisonResult`, sem levar
pandas, CSV ou Excel para os casos de uso e para o domínio.

Um adaptador PostgreSQL pode implementar as mesmas portas sem alterar os casos
de uso.

### `app/main.py`

Adaptador de entrada web. O FastAPI:

- recebe HTTP e formulários;
- salva temporariamente os uploads;
- cria comandos para os casos de uso;
- transforma resultados em respostas HTML ou downloads.

As regras de negócio não ficam nas rotas.

### `app/bootstrap`

`container.py` é o composition root. Ele escolhe as implementações concretas e
injeta os adaptadores nos casos de uso.

## Fluxo de uma conciliação

```text
POST /processar
    ↓
ProcessReconciliationCommand
    ↓
ProcessReconciliation (caso de uso)
    ├── UnitRepository (porta)
    ├── ReconciliationEngine (porta)
    └── ReconciliationRepository (porta)
             ↓
      Adaptadores pandas e SQLite
```

## Testabilidade

Os casos de uso podem receber repositórios e motores falsos em testes. Os
adaptadores SQLite e pandas também possuem testes próprios de integração.

## Limite do domínio

As regras de normalização e matching ainda operam internamente com DataFrames,
mas estão encapsuladas atrás da porta `ReconciliationEngine`. Portanto, pandas
não vaza para FastAPI, casos de uso ou entidades do domínio. Uma implementação
futura pode substituir pandas sem mudar as entradas do sistema.
