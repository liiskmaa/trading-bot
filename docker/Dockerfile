# Multi-stage build — keeps the final image small.
# Targets linux/arm64 for Raspberry Pi 4/5.
# Build: docker buildx build --platform linux/arm64 -t gridbot:latest .

FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools (needed for numpy/scikit-learn C extensions on ARM64).
# build-essential includes gcc + libc6-dev headers; without libc6-dev the
# meson build system reports "Compiler cc cannot compile programs."
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# Upgrade pip first so it can resolve manylinux aarch64 wheels correctly.
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefer-binary --user \
        --extra-index-url https://www.piwheels.org/simple \
        -r requirements.txt


FROM python:3.11-slim

WORKDIR /app

# Runtime dependency: piwheels numpy links against OpenBLAS at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopenblas0 \
    libatomic1 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /root/.local /root/.local

# Source code
COPY config/       config/
COPY core/         core/
COPY market_data/  market_data/
COPY grid_engine/  grid_engine/
COPY execution/    execution/
COPY risk/         risk/
COPY ai_filter/    ai_filter/
COPY ma_strategy/  ma_strategy/
COPY backtesting/  backtesting/
COPY database/     database/
COPY cache/        cache/
COPY monitoring/   monitoring/
COPY main.py       .

# Persistent state volumes
RUN mkdir -p data logs

ENV PATH=/root/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Health check via monitoring endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/status')" || exit 1

ENTRYPOINT ["python", "main.py"]
CMD []
