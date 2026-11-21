# Blast Radius submission image.
#
# Runtime: stdlib-only Python reads /data and prints one JSON line per commit.
# Build: downloads sqlglot at the pinned commit (the "old" side of every patch)
# and, only if the committed maps still mark some tests as untrusted, installs
# the labelling environment from requirements.lock and retraces those tests.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1
ARG SQLGLOT_COMMIT=eb11aec4be8cb2122afe713a4816662c3f0fb2fa

ADD https://github.com/tobymao/sqlglot/archive/${SQLGLOT_COMMIT}.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app/src \
 && tar -xzf /tmp/sqlglot.tar.gz -C /app/src --strip-components=1 \
 && rm /tmp/sqlglot.tar.gz

COPY requirements.lock /app/requirements.lock
COPY tools /app/tools
COPY maps /app/maps
# Optional, bounded and never fatal: see tools/finish_maps.py.
RUN python /app/tools/finish_maps.py --maps /app/maps/maps.bin --tests /app/maps/tests.json \
        --repo /app/src --requirements /app/requirements.lock --budget 200 \
 && rm -rf /tmp/finish /root/.cache

COPY selector /app/selector
COPY run.py /app/run.py
# Bytecode for the pinned sources makes the subtest replicas import sqlglot quickly.
RUN python -m compileall -q /app/selector /app/run.py /app/src/sqlglot /app/src/tests \
 && chmod -R a+rX /app

WORKDIR /app
CMD ["python", "-I", "-S", "/app/run.py"]
