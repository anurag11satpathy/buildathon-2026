# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Pinned sqlglot source (allowed: sqlglot's own repo at the pinned commit).
# The selector reads the original files to apply patches and syntax-check them.
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app/src \
 && tar -xzf /tmp/sqlglot.tar.gz -C /app/src --strip-components=1 \
 && rm /tmp/sqlglot.tar.gz \
 && test -f /app/src/sqlglot/__init__.py

COPY data/map.json.gz /app/data/map.json.gz
COPY data/import_funcs.json /app/data/import_funcs.json
COPY data/test_imports.json /app/data/test_imports.json
COPY data/dialect_children.json /app/data/dialect_children.json
COPY data/dialect_usage.json /app/data/dialect_usage.json
COPY selector.py /app/selector.py
# readable by user nobody at run time
RUN chmod -R a+rX /app

CMD ["python", "/app/selector.py"]
