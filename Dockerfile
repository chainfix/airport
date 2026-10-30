FROM goacme/lego:v4.26.0 AS lego
FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/chainfix/airport"
LABEL org.opencontainers.image.description="AirSIM NAS TLS gateway with Chinese setup panel"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 AIRPORT_DATA=/data HOME=/data
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources && apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=30 update && apt-get -o Acquire::Retries=3 -o Acquire::https::Timeout=30 install -y --no-install-recommends ca-certificates openssl && rm -rf /var/lib/apt/lists/* && groupadd -g 10001 airport && useradd -u 10001 -g airport -d /data -s /usr/sbin/nologin airport && mkdir /data && chown airport:airport /data
COPY --from=lego /lego /usr/local/bin/lego
WORKDIR /app
COPY pyproject.toml ./
COPY airport ./airport
RUN pip install --no-cache-dir .
USER 10001:10001
EXPOSE 8787 17595 17596
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)"
CMD ["python", "-m", "airport"]
