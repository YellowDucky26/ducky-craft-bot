FROM python:3.13-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY duckybot ./duckybot
ARG VERSION=dev
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data APP_VERSION=$VERSION
RUN mkdir /data && chown nobody /data
VOLUME /data
USER nobody
EXPOSE 8688
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD wget -qO- "http://127.0.0.1:${WEB_PORT:-8688}/health" >/dev/null || exit 1
CMD ["python3", "-m", "duckybot"]
