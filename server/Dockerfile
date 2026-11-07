FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8080
WORKDIR /app
COPY server.py ./
COPY static ./static

RUN useradd --uid 10001 --no-create-home app && mkdir -p /data && chown app /data
USER app
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

CMD ["python", "server.py"]
