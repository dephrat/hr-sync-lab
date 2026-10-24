# Deploying the demo page

The page runs as one container on the VPS behind Traefik, at `https://hrsync.ephrat.ai/<key>/`. The key is a random path prefix that Traefik strips, so the page is not linked from anywhere and is only reachable by someone who has the URL. The container seeds its own data on first start and keeps it in `/docker/hrsync/data`.

## First deploy

```
ssh root@2.25.77.143 "mkdir -p /docker/hrsync/app /docker/hrsync/data"
COPYFILE_DISABLE=1 git archive HEAD | ssh root@2.25.77.143 "tar -x -C /docker/hrsync/app"
scp deploy/docker-compose.yml root@2.25.77.143:/docker/hrsync/docker-compose.yml
ssh root@2.25.77.143 'test -f /docker/hrsync/.env || echo "HRSYNC_KEY=$(openssl rand -hex 12)" > /docker/hrsync/.env; chmod 600 /docker/hrsync/.env'
ssh root@2.25.77.143 "cd /docker/hrsync && docker compose up -d --build"
```

The key is generated on the VPS and lives only in `/docker/hrsync/.env`. Never commit it. To keep a copy on your Mac: `ssh root@2.25.77.143 "cat /docker/hrsync/.env" > ~/.hrsync-demo-key && chmod 600 ~/.hrsync-demo-key`.

Add a DNS A record `hrsync.ephrat.ai` pointing to `2.25.77.143`. Traefik requests the certificate once the name resolves.

## Redeploy after a change

Commit first (the archive is of `HEAD`), then from the repo root:

```
sh deploy/redeploy.sh
```

The `data` folder is untouched, so demo state survives. To start from fresh seed data, use Reset on the page, or `ssh root@2.25.77.143 "cd /docker/hrsync && rm -rf data/* && docker compose restart"`.

## Checks

```
ssh root@2.25.77.143 "cd /docker/hrsync && docker compose logs --tail 20"
```

The log shows the seed output on first start. Rotating the key: delete `/docker/hrsync/.env`, rerun the `.env` line above, then `docker compose up -d`.
