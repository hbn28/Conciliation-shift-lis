FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY app ./app
COPY run.py README.md ./

RUN mkdir -p /app/app/storage/uploads /app/app/storage/results

EXPOSE 8001

# Usa $PORT quando definido (Railway e outras PaaS injetam essa variável);
# mantém 8001 como padrão local/Docker Compose.
# --proxy-headers + --forwarded-allow-ips="*": o Railway (e PaaS parecidas)
# terminam o HTTPS na borda e repassam a requisição como HTTP simples pro
# container. Sem essas flags, o Uvicorn acha que a conexão é HTTP e gera
# links (como o do CSS) com "http://", que o navegador bloqueia por
# conteúdo misto numa página servida em HTTPS.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8001} --proxy-headers --forwarded-allow-ips='*'"]
