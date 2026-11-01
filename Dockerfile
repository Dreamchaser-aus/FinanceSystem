FROM postgres:18-bookworm AS pgtools
FROM python:3.11-slim-bookworm
WORKDIR /app
COPY --from=pgtools /usr/lib/postgresql/18/bin/pg_dump /usr/local/bin/pg_dump
COPY --from=pgtools /usr/lib/postgresql/18/bin/pg_restore /usr/local/bin/pg_restore
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 libssl3 libzstd1 liblz4-1 zlib1g ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "start.sh"]
