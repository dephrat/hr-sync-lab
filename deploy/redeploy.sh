#!/bin/sh
# Push the committed HEAD to the VPS and rebuild. Run from the repo root.
set -e
: "${HRSYNC_VPS:?set HRSYNC_VPS=user@host}"
HOST=$HRSYNC_VPS
ssh $HOST "mkdir -p /docker/hrsync/app /docker/hrsync/data"
COPYFILE_DISABLE=1 git archive HEAD | ssh $HOST "tar -x -C /docker/hrsync/app"
scp deploy/docker-compose.yml $HOST:/docker/hrsync/docker-compose.yml
ssh $HOST "cd /docker/hrsync && docker compose up -d --build"
