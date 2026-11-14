article-06 observability lab (r2)
================================

What this is
------------
The article-5 action lab (draft -> page confirmation -> simulated pending-review
ticket) with tracing, metrics and joinable logs added. Business rules are
unchanged; this directory only adds observation.

Services (loopback only)
------------------------
  8094  action API + page           (action_api:app)
  8095  simulated ticket service    (mcp_action_server:app)
 16686  Jaeger UI
  4317  OTLP gRPC (Jaeger)
  4318  OTLP HTTP (Jaeger)

Storage
-------
State (SQLite, credentials, run files, evidence) lives outside the code
directory, in STATE_DIR (default /root/rag-lab/article06-state). Replacing the
code directory must not move or recreate the database or the keys.

Boundaries, stated up front
---------------------------
* Traces carry identifiers, counts, durations, statuses and error classes.
  They never carry the question, the draft payload, ticket text, keys or
  confirmation ids (a confirmation id is a capability: it can be used to submit).
* Jaeger runs with in-memory storage. Restarting it loses every trace. Export
  before stopping it if the traces matter.
* Collection can be switched off with OTEL_ENABLED=0; business behaviour is
  identical, the only difference is that no spans are exported.
* No enterprise SSO, no real ServiceNow, no approval workflow: the ticket ends
  in pending_review by design.

Layout
------
  action_api.py           HTTP surface, page, confirmation and submission logic
  mcp_action_server.py    simulated ticket service (MCP over HTTP)
  model_loop.py           bounded tool loop against OGX/vLLM
  docsearch.py            document lookup against the version service (8089)
  draft.py / store.py     draft content, canonical text, SQLite persistence
  telemetry.py            tracer setup, span helpers, JSON log formatter
  metrics.py              small Prometheus text exporter
  deploy/                 env template and three systemd units
  scripts/install06.sh    install or reinstall (adopts an existing config)
  scripts/rollback06.sh   stop and remove units, keep state and config
  scripts/selftest_install06.sh  isolated install/rollback contract checks
  scripts/e06r2.py        E1-E6, L1, M1 runner (real services)
  scripts/e07_collect.py  collection on/off comparison, exploratory
  tests/                  pytest suite (see DEPLOYMENT.txt)

Dependencies
------------
requirements-otel.lock is a hash-pinned lock (uv, python 3.12, linux x86_64).
It pins the article-5 baseline plus opentelemetry-sdk, the OTLP HTTP exporter
and the FastAPI/HTTPX instrumentations at 1.45.2 / 0.66b2.
requirements-test.lock pins the test tools separately.

Start here: DEPLOYMENT.txt.
