FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Run unprivileged; /data (the working directory) holds the SQLite database.
RUN useradd --system --no-create-home netpulse \
    && mkdir /data \
    && chown netpulse /data
USER netpulse
WORKDIR /data

ENV NETPULSE_CONFIG=/etc/netpulse/config.yaml
EXPOSE 9100
CMD ["netpulse", "run"]
