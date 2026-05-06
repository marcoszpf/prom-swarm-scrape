FROM python:3.14.4-alpine
ENV TZ="America/Sao_Paulo"
ENV PYTHONPATH=/usr/lib/python3.12/site-packages

RUN apk update \
    && apk add --no-cache py3-docker-py \
    && mkdir -p /app

WORKDIR /app
COPY prom-swarm-scrape.py .

CMD ["/app/prom-swarm-scrape.py","--port", "8080"]
