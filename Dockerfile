FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TAILSAMPLER_HOST=0.0.0.0 \
    TAILSAMPLER_DATABASE_PATH=/var/lib/tailsampler/spans.sqlite3

WORKDIR /app

COPY requirements.lock pyproject.toml LICENSE ./
COPY src ./src
RUN pip install -c requirements.lock .

COPY configs/gateway.toml ./configs/gateway.toml
COPY results/thresholds.json ./results/thresholds.json

RUN useradd --system --no-create-home tailsampler \
    && mkdir -p /var/lib/tailsampler \
    && chown tailsampler /var/lib/tailsampler

USER tailsampler
VOLUME /var/lib/tailsampler
EXPOSE 4318

CMD ["tailsampler", "serve"]
