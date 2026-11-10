FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY nosi_tempo ./nosi_tempo

ENV DB_PATH=/data/tempo.db
VOLUME /data
CMD ["python", "-m", "nosi_tempo"]
