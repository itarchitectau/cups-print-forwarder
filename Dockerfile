FROM python:3.12-slim

# System deps: libcups for pycups, LibreOffice for DOCX->PDF conversion
RUN apt-get update && apt-get install -y --no-install-recommends \
        libcups2-dev \
        libreoffice-writer \
        libreoffice-common \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py config.py run.py ./
COPY templates/ templates/

RUN mkdir -p uploads \
    && useradd --system --no-create-home --shell /bin/false appuser \
    && chown -R appuser:appuser /app

USER appuser

ENV FLASK_HOST=0.0.0.0 \
    FLASK_PORT=5000 \
    CUPS_HOST=host.docker.internal \
    CUPS_PORT=631 \
    LIBREOFFICE_BIN=soffice

EXPOSE 5000

CMD ["python", "run.py"]
