#!/bin/sh
set -e
if [ ! -f /app/data/hris.db ]; then
  hrsync seed
fi
exec hrsync web
