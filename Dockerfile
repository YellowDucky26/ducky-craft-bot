FROM python:3.13-alpine
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY duckybot ./duckybot
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN mkdir /data && chown nobody /data
VOLUME /data
USER nobody
EXPOSE 8688
CMD ["python3", "-m", "duckybot"]
