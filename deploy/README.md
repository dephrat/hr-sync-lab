# Deploying the demo page

The page runs as one container on the VPS behind Traefik, at `https://hrsync.ephrat.ai/<key>/`. The key is a random path prefix that Traefik strips, so the page is not linked from anywhere and is only reachable by someone who has the URL. The container seeds its own data on first start and keeps it in `/docker/hrsync/data`.

Set `HRSYNC_VPS` to your SSH target (for example `root@your-host`) in the shell first: `export HRSYNC_VPS=root@your-host`. Nothing in this repo names the server.

## First deploy

```
ssh $HRSYNC_VPS "mkdir -p /docker/hrsync/app /docker/hrsync/data"
COPYFILE_DISABLE=1 git archive HEAD | ssh $HRSYNC_VPS "tar -x -C /docker/hrsync/app"
scp deploy/docker-compose.yml $HRSYNC_VPS:/docker/hrsync/docker-compose.yml
ssh $HRSYNC_VPS 'test -f /docker/hrsync/.env || echo "HRSYNC_KEY=$(openssl rand -hex 12)" > /docker/hrsync/.env; chmod 600 /docker/hrsync/.env'
ssh $HRSYNC_VPS "cd /docker/hrsync && docker compose up -d --build"
```

The key is generated on the VPS and lives only in `/docker/hrsync/.env`. Never commit it. To keep a copy on your Mac: `ssh $HRSYNC_VPS "cat /docker/hrsync/.env" > ~/.hrsync-demo-key && chmod 600 ~/.hrsync-demo-key`.

Add a DNS A record `hrsync.ephrat.ai` pointing to the VPS address. Traefik requests the certificate once the name resolves.

## Redeploy after a change

Commit first (the archive is of `HEAD`), then from the repo root:

```
HRSYNC_VPS=root@your-host sh deploy/redeploy.sh
```

The `data` folder is untouched, so demo state survives. To start from fresh seed data, use Reset on the page, or `ssh $HRSYNC_VPS "cd /docker/hrsync && rm -rf data/* && docker compose restart"`.

## Checks

```
ssh $HRSYNC_VPS "cd /docker/hrsync && docker compose logs --tail 20"
```

The log shows the seed output on first start. Rotating the key: delete `/docker/hrsync/.env`, rerun the `.env` line above, then `docker compose up -d`.

## Notes on the demo data

The page assumes `ETHENA_API_KEY` is left at its default `test-key`, because the page talks to the mock Ethena with that fixed demo key. Anyone with the URL can change the demo data (it is all fake); Reset on the page restores it.
