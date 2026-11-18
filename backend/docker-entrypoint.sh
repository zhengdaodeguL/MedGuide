#!/bin/sh
set -eu

knowledge_backend="${MEDGUIDE_KNOWLEDGE_BACKEND:-milvus}"
index_path="${BM25_INDEX_PATH:-/app/data/runtime/bm25-index.json}"

# Production deployments need external services that /api/ready would
# otherwise only report as missing: without them the API stays up but
# never becomes ready, so the UI (depends_on: service_healthy) and the
# gateway never start and the whole stack sits unhealthy with no clear
# error. Fail here, before ingestion and before the server starts,
# naming every missing variable (issue #20). This is deliberately an
# entrypoint check and not a ${VAR:?} interpolation in docker-compose.yml:
# MILVUS_URI is only required for the milvus backend, the auth database
# has an explicit SQLite escape hatch, and CI runs `docker compose config`
# with no environment set, so compose-level interception cannot express
# the conditions and would break validation. Local/offline/test modes are
# unaffected: they use their own providers and never reach this branch.
mode="$(printf '%s' "${MEDGUIDE_MODE:-production}" | tr '[:upper:]' '[:lower:]')"
if [ "$mode" = "production" ]; then
  python - <<'PY'
import os

missing: list[tuple[str, str]] = []


def _present(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def _truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


if not _present("OPENAI_API_KEY"):
    missing.append(("OPENAI_API_KEY", "the OpenAI answerer has no credential"))
if not _present("OPENAI_MODEL"):
    missing.append(("OPENAI_MODEL", "model selection is part of the production provider contract"))
if not _present("REDIS_URL"):
    missing.append(("REDIS_URL", "sessions, idempotency and metrics need Redis in production"))
backend = (os.environ.get("MEDGUIDE_KNOWLEDGE_BACKEND") or "").strip().lower() or "milvus"
if backend == "milvus" and not _present("MILVUS_URI"):
    missing.append(("MILVUS_URI", "the milvus knowledge backend needs a vector store address"))
auth_backend = (os.environ.get("MEDGUIDE_AUTH_BACKEND") or "mysql").strip().lower()
sqlite_auth_allowed = auth_backend == "sqlite" and _truthy("MEDGUIDE_ALLOW_SQLITE_AUTH")
if not _present("MEDGUIDE_AUTH_DSN") and not sqlite_auth_allowed:
    missing.append((
        "MEDGUIDE_AUTH_DSN",
        "the shared auth database is required when MEDGUIDE_AUTH_BACKEND=mysql; "
        "set MEDGUIDE_AUTH_BACKEND=sqlite and MEDGUIDE_ALLOW_SQLITE_AUTH=true "
        "to use the explicit single-node SQLite auth instead",
    ))

if missing:
    lines = ["MedGuide production deployment is missing required configuration:"]
    lines.extend(f"  - {name}: {reason}" for name, reason in missing)
    lines.append("Set the missing variables in .env (see .env.template) and re-run `docker compose up --build -d`.")
    raise SystemExit("\n".join(lines))
PY
fi

# The API receives forwarded HTTPS headers from the UI container. In the
# Compose topology that container lives inside MEDGUIDE_NETWORK_SUBNET, so a
# missing, empty, malformed, or drifting trust boundary must fail before the
# server starts rather than turning into a runtime 426/500 surprise.
if [ "${MEDGUIDE_MODE:-production}" != "offline" ] && [ "${MEDGUIDE_MODE:-production}" != "test" ]; then
  python - <<'PY'
import ipaddress
import os

expected_raw = os.environ.get("MEDGUIDE_NETWORK_SUBNET", "").strip()
trusted_raw = os.environ.get("MEDGUIDE_API_TRUSTED_PROXY_CIDR", "")
if not expected_raw:
    raise SystemExit("MEDGUIDE_NETWORK_SUBNET must be set in production")
if not trusted_raw.strip():
    raise SystemExit("MEDGUIDE_API_TRUSTED_PROXY_CIDR must be set when MEDGUIDE_NETWORK_SUBNET is set")

try:
    expected = ipaddress.ip_network(expected_raw, strict=False)
    trusted = tuple(
        ipaddress.ip_network(item.strip(), strict=False)
        for item in trusted_raw.split(",")
        if item.strip()
    )
except ValueError as exc:
    raise SystemExit(f"invalid trusted proxy/network CIDR: {exc}") from exc

if len(trusted) != 1 or trusted[0] != expected:
    raise SystemExit(
        "MEDGUIDE_API_TRUSTED_PROXY_CIDR must contain the exact "
        "MEDGUIDE_NETWORK_SUBNET for the Compose UI-to-API proxy"
    )
PY
fi

case "$knowledge_backend" in
  milvus)
    mkdir -p "$(dirname "$index_path")"
    python -m app.ingestion \
      --catalog /app/data/knowledge/catalog.json \
      --bm25-index "$index_path"
    ;;
  bm25)
    mkdir -p "$(dirname "$index_path")"
    python /app/scripts/build_bm25_index.py \
      --catalog /app/data/knowledge/catalog.json \
      --output "$index_path" \
      --if-stale
    ;;
esac

exec "$@"
