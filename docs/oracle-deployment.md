# Oracle Cloud Always Free deployment

This deployment keeps the complete productincluding Docker-isolated code
executionon one ARM64 Ubuntu VM. Caddy provides automatic HTTPS and routes the
browser to the production frontend, FastAPI, and the collaboration WebSocket.
PostgreSQL and Redis have no host ports.

## 1. Create the VM

In the Oracle Cloud console, create an Always Free-eligible
`VM.Standard.A1.Flex` instance in your home region:

- Ubuntu 24.04, ARM64
- 2 OCPUs and 12 GB memory
- 80100 GB boot volume
- A public IPv4 address
- Your SSH public key

Oracle can temporarily run out of free A1 capacity in a region, and idle free
instances can be reclaimed. These are platform constraints, not application
failures. See the [current Always Free limits](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

## 2. Lock down networking

In the OCI subnet security list (or network security group), allow only:

- TCP 22 from your own IP address
- TCP 80 from `0.0.0.0/0` and `::/0`
- TCP 443 from `0.0.0.0/0` and `::/0`
- UDP 443 from `0.0.0.0/0` and `::/0` (optional HTTP/3)

Do **not** expose ports 2375, 5432, 6379, or 8000.

## 3. Install Docker

Connect over SSH, then install Docker and the Compose plugin using Docker's
[official Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/).
Also install Git and configure the host firewall:

```bash
sudo apt update
sudo apt install -y git ufw
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp
sudo ufw --force enable
sudo usermod -aG docker "$USER"
```

Log out and reconnect so the Docker group membership takes effect. Verify with
`docker version` and `docker compose version`.

## 4. Configure Concord

```bash
git clone https://github.com/adityak1609/Code_Collaborator.git
cd Code_Collaborator/deploy/oracle
cp .env.example .env
openssl rand -hex 32
openssl rand -hex 32
```

Put the two different generated values into `POSTGRES_PASSWORD` and
`JWT_SECRET`. Use only URL-safe characters for `POSTGRES_PASSWORD`.

For a domain you control, create an `A` record pointing to the VM and set
`DOMAIN` to that hostname. For a no-cost temporary hostname, set it to
`PUBLIC_IP.sslip.io`, such as `203.0.113.10.sslip.io`. Set `ACME_EMAIL` to your
real email so Caddy can manage certificate notices.

Never commit `.env`; repository ignore rules already exclude it.

## 5. Deploy

```bash
docker compose --env-file .env up -d --build
docker compose ps
docker compose logs -f caddy backend worker
```

Open `https://$DOMAIN`. Caddy obtains and renews the TLS certificate
automatically. The first execution for each language can be slower while its
sandbox image is pulled.

## 6. Update and operate

```bash
git pull --ff-only
cd deploy/oracle
docker compose --env-file .env up -d --build
docker image prune -f
```

Create a PostgreSQL backup before significant updates:

```bash
docker compose --env-file .env exec -T postgres \
  pg_dump -U concord -d concord > "concord-$(date +%F).sql"
```

Inspect health and resource use with:

```bash
docker compose ps
docker stats --no-stream
curl -fsS "https://$DOMAIN/api/health"
```

## Public-demo safety

The production example lowers execution time, memory, process, and output
limits. The worker is trusted and has Docker-daemon control; user sandboxes do
not receive the Docker socket and retain the application's network, filesystem,
capability, CPU, memory, and PID restrictions. Keep the OS and Docker patched,
monitor disk usage, back up PostgreSQL, and shut the demo down if it attracts
unwanted automated traffic.
