FROM python:3.11-slim

# Fuso de SP para o carimbo de data/hora da manifestação sair correto
ENV TZ=America/Sao_Paulo
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Volumes montados pelo compose: certificado (ro), saída dos XML e estado/índice
VOLUME ["/certs", "/data", "/state"]

ENV PYTHONUNBUFFERED=1

# Padrão: API. A rotina agendada sobe com outro command no compose.
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
