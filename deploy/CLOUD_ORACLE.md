# Always-on cloud server (Oracle Cloud Always-Free)

Run the whole system on a free Linux VM so it controls your Pod 24/7 — independent of your home
PC, your home internet, or whether anything is running on your computer. The controller talks to
the Eight Sleep cloud, so it drives the bed from up there every night automatically; the dashboard
is reachable from your phone over HTTPS anywhere.

**What it costs:** $0. Oracle's Always-Free tier is genuinely free; a credit card is required only
for identity verification and won't be charged as long as you stay on free resources.

**Effort:** ~20–30 min, one time. The hard part (getting a VM) is below; the deploy itself is a
single command.

---

## Part 1 — Create the VM (only you can do this)

1. Sign up at **https://www.oracle.com/cloud/free/** → "Start for free." Pick a home region close
   to you. Verify email + card.
2. In the console: **Menu → Compute → Instances → Create Instance**.
   - **Image:** Canonical **Ubuntu 22.04**.
   - **Shape:** click *Change shape* → **Ampere (ARM)** → `VM.Standard.A1.Flex`, set **1 OCPU /
     6 GB** (well within free). If ARM says "out of capacity," try another region or use
     `VM.Standard.E2.1.Micro` (AMD, always available — but only 1 GB RAM; see the note at the end).
   - **SSH keys:** choose **Generate a key pair for me** and **download the private key** (e.g.
     `ssh-key.key`). Keep it safe.
   - Leave networking default (a public IP is assigned). **Create.**
3. When it's **Running**, copy its **Public IP address**.

> Networking note: you do **not** need to open any ports / security lists / firewall — this deploy
> uses a Cloudflare tunnel that connects *outbound*, so the VM stays closed to the internet.

---

## Part 2 — Connect to it (from your Windows PC)

Open **PowerShell** and SSH in (Windows has `ssh` built in). Replace the key path + IP:

```powershell
icacls "$HOME\Downloads\ssh-key.key" /inheritance:r /grant:r "$($env:USERNAME):(R)"   # lock down the key
ssh -i "$HOME\Downloads\ssh-key.key" ubuntu@YOUR.PUBLIC.IP
```

Type `yes` to accept the host key. You're now on the server (prompt shows `ubuntu@...`).

---

## Part 3 — Deploy (one command on the VM)

```bash
curl -fsSL https://raw.githubusercontent.com/dgkenn/SleepController/main/scripts/cloud-setup.sh | bash
```

It installs Docker, builds the stack, starts a Cloudflare tunnel, and prints:
- a **dashboard URL** like `https://something-random.trycloudflare.com`
- your **login** (`admin` / a generated password, saved in `~/SleepController/deploy/.env`).

Open that URL on your iPhone, log in, **Share → Add to Home Screen**. It's HTTPS and works from
anywhere.

---

## Part 4 — Point it at your Pod

The daemon starts in **dry-run** (read-only) and needs your Eight Sleep login. On the VM:

```bash
nano ~/SleepController/deploy/.env
```

Uncomment and fill these four lines (arrow-key down, delete the `#`), then **Ctrl+O, Enter, Ctrl+X**:
```
EIGHTSLEEP_EMAIL=you@example.com
EIGHTSLEEP_PASSWORD=your-password
EIGHTSLEEP_TIMEZONE=America/New_York
EIGHTSLEEP_SIDE=right
```

Apply it:
```bash
cd ~/SleepController/deploy
docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d daemon
```

The Admin page should now show "Live (real Pod)" with a dry-run badge. Watch a night read-only,
then go live by setting `SLEEPCTL_DRY_RUN=0` in `.env` and re-running that `up -d daemon` command.

---

## Living with it

- **It auto-restarts** (Docker `restart: unless-stopped`) on crash and on VM reboot — nothing to
  babysit.
- **The tunnel URL changes only if cloudflared restarts** (e.g. a VM reboot — rare). To get the
  current one: `cd ~/SleepController/deploy && docker compose -f docker-compose.yml -f docker-compose.cloud.yml logs cloudflared | grep trycloudflare`. (Want a permanent URL? Add a free DuckDNS domain later — ask and I'll wire Caddy auto-HTTPS.)
- **Update to the latest code:** `cd ~/SleepController && git pull && cd deploy && docker compose -f docker-compose.yml -f docker-compose.cloud.yml up -d --build`.
- **Security:** the dashboard is login-protected and `BCG_INGEST_OPEN=0` (the phone-sensor endpoint needs a token over the public internet). Your Eight Sleep creds live in `deploy/.env` on the VM — keep the SSH key private, and consider changing that password periodically.

> If you picked the 1 GB AMD micro shape, the web image build can run out of memory. Add swap first:
> `sudo fallocate -l 2G /swap && sudo chmod 600 /swap && sudo mkswap /swap && sudo swapon /swap`,
> then re-run the deploy. The 6 GB ARM shape needs none of this.
