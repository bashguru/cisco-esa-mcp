FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/data/models \
    HF_HUB_CACHE=/data/models \
    PYTHONPATH=/app/src

WORKDIR /app

# System libs: docling needs OpenGL + poppler; OCR needs tesseract.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgl1 \
        libglib2.0-0 \
        poppler-utils \
        tesseract-ocr \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install the CPU build of torch first so we don't pull huge CUDA wheels.
# For a GPU box, remove this line and install a CUDA torch build instead.
RUN pip install --upgrade pip && \
    pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts
COPY config ./config
RUN pip install -e . --no-deps

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=5 \
    CMD python /app/scripts/healthcheck.py

CMD ["python", "-m", "cisco_mcp.server"]
