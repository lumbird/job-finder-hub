# Deploying to the Pi

The hub runs on the Pi in Docker, on the Pi's existing `ipvlan_sub_network` at **http://10.254.0.20/**.
Develop on the PC; the Pi has a bare git repo you push to.

## First time (already done 27 Oct 2026)

```bash
# on the Pi
git init --bare ~/git/job-finder-hub.git
git clone ~/git/job-finder-hub.git ~/apps/job-finder-hub
# copy .env and data/ into ~/apps/job-finder-hub (data holds hub.db, CVs, criteria.yaml, backups)
```

`.env` on the Pi needs `HUB_PUBLIC_ORIGIN=http://10.254.0.20` and `HUB_COOKIE_SECURE=false`
(plain HTTP on the home network). Keep the same `HUB_FERNET_KEY`, or saved job-site keys can't be read.

## Deploy a change

```bash
# on the PC
git push pi main
# on the Pi
cd ~/apps/job-finder-hub && git pull && sudo docker compose up -d --build
```

Logs: `sudo docker logs -f job-finder-hub`. Data lives in `~/apps/job-finder-hub/data` on the Pi;
the nightly backup goes to `data/backups/`. Copy it off the SD card too (see the roadmap).
