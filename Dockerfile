FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Запуск без root; в /data (рабочая директория) лежит база SQLite.
RUN useradd --system --no-create-home netpulse \
    && mkdir /data \
    && chown netpulse /data
USER netpulse
WORKDIR /data

ENV NETPULSE_CONFIG=/etc/netpulse/config.yaml
EXPOSE 9100
CMD ["netpulse", "run"]
