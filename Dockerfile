FROM python:3.13-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV CLOUDTEC_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
RUN useradd --system --uid 10001 cloudtec && mkdir -p /data && chown cloudtec /data
USER cloudtec

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
