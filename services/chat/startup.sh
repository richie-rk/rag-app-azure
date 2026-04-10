#!/bin/bash
# Gunicorn startup for Azure App Service.
# Fix over Max AI: uses (2 * CPU) + 1 workers instead of 1.

WORKERS=${WEB_CONCURRENCY:-$(( 2 * $(nproc) + 1 ))}

exec gunicorn main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "$WORKERS" \
    --bind 0.0.0.0:8000 \
    --timeout 600 \
    --access-logfile - \
    --error-logfile -
