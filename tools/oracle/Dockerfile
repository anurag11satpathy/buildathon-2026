# Offline only (never submitted): ground-truth "oracle" for the mutation suite.
# Same base image as the scorer, with sqlglot's test dependencies installed.
# For each patch it applies it to a fresh copy of pinned sqlglot and runs the
# full pytest suite, recording which tests fail. See tools/mutation_suite.py.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /requirements.lock
RUN pip install -q --root-user-action=ignore -r /requirements.lock

ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /pristine \
 && tar -xzf /tmp/sqlglot.tar.gz -C /pristine --strip-components=1 \
 && rm /tmp/sqlglot.tar.gz \
 && test -f /pristine/sqlglot/__init__.py

COPY failplugin.py run_oracle.py /oracle/
ENV PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0
CMD ["python", "/oracle/run_oracle.py"]
