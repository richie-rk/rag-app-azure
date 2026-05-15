#!/bin/bash
# Gunicorn startup for Azure App Service.

WORKERS=${WEB_CONCURRENCY:-$(( 2 * $(nproc) + 1 ))}

exec gunicorn main:app \
    --worker-class uvicorn.workers.UvicornWorker \
    --workers "$WORKERS" \
    --bind 0.0.0.0:8000 \
    --timeout 600 \
    --access-logfile - \
    --error-logfile -
