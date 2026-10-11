#!/bin/sh
set -eu

# The API owns the control database schema. Running migrations before the
# server starts keeps a plain `docker compose up` deployment usable as well as
# the explicit bootstrap workflow. When Compose invokes Alembic explicitly,
# pass that command through without running it twice.
if [ "${1:-}" != "alembic" ]; then
  alembic upgrade head
fi

exec "$@"
