# ducky-craft-bot

[![Docker](https://github.com/YellowDucky26/ducky-craft-bot/actions/workflows/docker.yml/badge.svg)](https://github.com/YellowDucky26/ducky-craft-bot/actions/workflows/docker.yml)
[![Release](https://img.shields.io/github/v/release/YellowDucky26/ducky-craft-bot)](https://github.com/YellowDucky26/ducky-craft-bot/releases)

Discord-bot voor de **Ducky-Craft**-server. Hij koppelt Discord aan
Jellyseerr en Jellyfin:

- **Media aanvragen** met `/aanvraag`: zoeken met autocomplete, een kaart met
  poster en status, en één klik op *Aanvragen*. De aanvraag komt in Jellyseerr
  op naam van de juiste gebruiker, met diens eigen rechten, quota en
  auto-approve.
- **Uitnodigen** met `/invite discord` (een invitelink) of `/invite jellyfin`
  (maakt een Jellyfin-account, importeert het in Jellyseerr, koppelt het aan
  het Discord-lid en stuurt de inloggegevens per DM).
- **Notificaties**:
  - Meldingen van Jellyseerr via een webhook. Een nieuwe aanvraag komt in het
    adminkanaal met de knoppen **Goedkeuren** en **Afwijzen**. *Goedgekeurd* en
    *Nu beschikbaar* komen in het aanvraagkanaal en gaan ook per DM naar de
    aanvrager, met een knop *Kijk in Jellyfin*.
  - `/melding` voor aankondigingen, met een optionele rol- of @everyone-ping.
  - `POST /notify` zodat andere diensten (Minecraft-server, scripts, ...) via
    de bot een bericht kunnen sturen.

Zusterproject van
[proxmox-discord-relay](https://github.com/YellowDucky26/proxmox-discord-relay) en
[truenas-discord-relay](https://github.com/YellowDucky26/truenas-discord-relay).

## Commando's

| Commando | Wie | Wat |
|---|---|---|
| `/aanvraag titel:` | iedereen | Film of serie zoeken en aanvragen. Bij series worden alle ontbrekende seizoenen aangevraagd. |
| `/mijn-aanvragen` | iedereen | Je laatste 10 aanvragen met status. |
| `/koppeling` | iedereen | Aan welk Jellyseerr-account ben je gekoppeld? |
| `/invite discord [max_gebruik] [geldig]` | invite-rol / admin | Invitelink, standaard 1× te gebruiken en 1 dag geldig. |
| `/invite jellyfin lid: [gebruikersnaam]` | invite-rol / admin | Jellyfin-account voor een lid. |
| `/koppel lid: gebruiker:` | admin | Discord-lid handmatig aan een Jellyseerr-account koppelen. |
| `/ontkoppel lid:` | admin | Koppeling verwijderen. |
| `/melding [kanaal] [ping]` | admin | Aankondiging versturen; titel en tekst vul je in een venster in. |

**Admin** is iedereen met het recht *Server beheren* of een rol uit
`ADMIN_ROLE_IDS`. `/koppel`, `/ontkoppel` en `/melding` zijn standaard alleen
zichtbaar voor *Server beheren*. Dat kun je per rol aanpassen onder
Serverinstellingen → Integraties → bot.

### Hoe weet de bot wie wie is?

Een aanvraag moet in Jellyseerr op de juiste naam komen. De bot zoekt de
Jellyseerr-gebruiker bij een Discord-lid op deze manieren, in deze volgorde:

1. Een eerder opgeslagen koppeling (`data/links.json`).
2. Het Discord-ID in het Jellyseerr-profiel (*Profiel → Instellingen →
   Notificaties → Discord*). Leden kunnen dit zelf invullen.
3. Wie via `/invite jellyfin` binnenkomt, wordt automatisch gekoppeld.
4. Een admin koppelt met `/koppel`. Dat zet het Discord-ID ook meteen in het
   Jellyseerr-profiel.

Zonder koppeling legt de bot uit hoe je die maakt. Er wordt dan niets op
naam van de admin aangevraagd.

## Installatie

### 1. Discord-applicatie

1. Ga naar <https://discord.com/developers/applications> → **New Application**.
2. Kies **Bot** → **Reset Token** en kopieer het token (`DISCORD_TOKEN`).
   Privileged intents zijn niet nodig.
3. Kies **OAuth2 → URL Generator**: scopes `bot` en `applications.commands`,
   met deze rechten: *View Channels*, *Send Messages*, *Embed Links*,
   *Create Instant Invite*, en *Manage Roles* als je `MEMBER_ROLE_ID` gebruikt.
   Je kunt ook deze link gebruiken, met je eigen client-ID:
   `https://discord.com/oauth2/authorize?client_id=JOUW_ID&scope=bot+applications.commands&permissions=268454913`
4. Zet in Discord de ontwikkelaarsmodus aan (Instellingen → Geavanceerd). Daarna
   kun je met rechtsklik → *ID kopiëren* de server-, kanaal- en rol-ID's
   ophalen.

Staat `MEMBER_ROLE_ID` aan, sleep dan de rol van de bot **boven** die rol in de
rollenlijst. Anders mag de bot de rol niet toekennen.

### 2. API-sleutels

- **Jellyseerr**: Instellingen → Algemeen → *API Key* (`JELLYSEERR_API_KEY`).
- **Jellyfin** (alleen voor `/invite jellyfin`): Dashboard → API-sleutels → `+`
  (`JELLYFIN_API_KEY`).

### 3. Bot starten

Kopieer `bot.env.example` en vul de waarden in. Verplicht zijn `DISCORD_TOKEN`,
`GUILD_ID`, `JELLYSEERR_URL`, `JELLYSEERR_API_KEY` en `WEBHOOK_SECRET` (een
lange willekeurige string, bv. `openssl rand -hex 24`).

De bot is er als kant-en-klare image: `ghcr.io/yellowducky26/ducky-craft-bot`
(amd64 en arm64).

| Tag | Wat |
|---|---|
| `latest` | de laatste release (aanrader) |
| `1`, `1.2`, `1.2.3` | vastzetten op een (hoofd)versie |
| `main` | elke push naar `main`: nieuwste, maar nog niet uitgebracht |

#### Met Docker Compose (aanrader)

Zet `compose.yaml` en je ingevulde `bot.env` in dezelfde map en start:

```bash
docker compose up -d
```

Updaten naar de nieuwste release:

```bash
docker compose pull && docker compose up -d
```

De data (`links.json`) staat in het volume `ducky-bot-data` en blijft bij een
update bewaard.

#### Met `docker run`

```bash
docker run -d --name ducky-craft-bot --restart unless-stopped \
  --env-file bot.env -e DATA_DIR=/data -v ducky-bot-data:/data -p 8688:8688 \
  ghcr.io/yellowducky26/ducky-craft-bot:latest
```

Of zelf bouwen: `docker build -t ducky-craft-bot .`

#### Zonder Docker (systemd)

```bash
sudo useradd --system --no-create-home ducky-bot
sudo git clone https://github.com/YellowDucky26/ducky-craft-bot /opt/ducky-craft-bot
sudo python3 -m venv /opt/ducky-craft-bot/.venv
sudo /opt/ducky-craft-bot/.venv/bin/pip install -r /opt/ducky-craft-bot/requirements.txt
sudo install -d -m 700 /etc/ducky-craft-bot
sudo install -m 600 bot.env.example /etc/ducky-craft-bot/bot.env   # en invullen
sudo cp /opt/ducky-craft-bot/ducky-craft-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ducky-craft-bot
```

De standaardpoort is **8688**. Zo kan de bot naast de Proxmox-relay (8686) en
de TrueNAS-relay (8687) op dezelfde host draaien.

### 4. Jellyseerr-webhook

Instellingen → Notificaties → **Webhook**:

| Veld | Waarde |
|---|---|
| Webhook-URL | `http://<bot-host>:8688/webhook/jellyseerr` |
| Authorization Header | dezelfde waarde als `WEBHOOK_SECRET` |
| JSON Payload | de inhoud van [`jellyseerr-webhook.json`](jellyseerr-webhook.json) |
| Meldingstypes | wat je wilt; minimaal *Aanvraag in afwachting*, *Goedgekeurd*, *Beschikbaar* en *Afgewezen* |

Druk op **Test**: er moet een testmelding in het adminkanaal verschijnen.

> Gebruik de webhook **in plaats van** de ingebouwde Discord-agent van
> Jellyseerr. Anders krijg je elke melding twee keer.

Welk bericht gaat waarheen:

| Jellyseerr-melding | Adminkanaal | Aanvraagkanaal | DM aanvrager |
|---|:-:|:-:|:-:|
| Nieuwe aanvraag (wacht op goedkeuring) | ✅ met knoppen | | |
| Automatisch goedgekeurd | | ✅ | |
| Goedgekeurd | | ✅ | ✅ |
| Nu beschikbaar | | ✅ | ✅ |
| Afgewezen | ✅ | | ✅ |
| Mislukt, problemen (issues), test | ✅ | | |

`ADMIN_CHANNEL_ID` leeg? Dan gaan adminberichten naar `REQUEST_CHANNEL_ID`.

### 5. Meldingen van andere diensten (`/notify`)

```bash
curl -X POST http://<bot-host>:8688/notify \
  -H "Authorization: Bearer $WEBHOOK_SECRET" -H "Content-Type: application/json" \
  -d '{"title": "Minecraft", "message": "De server is weer online! 🦆", "color": "#57f287"}'
```

| Veld | |
|---|---|
| `title` / `message` | minstens één van de twee is verplicht |
| `channel_id` | standaard `ANNOUNCE_CHANNEL_ID` |
| `user_id` | stuurt een DM in plaats van een kanaalbericht |
| `mention_role_id` | pingt deze rol |
| `color` | `"#rrggbb"` of een getal |
| `url`, `image` | titel-link en thumbnail |

`GET /health` geeft `{"ok": true, "discord": true, "version": "1.0.0"}` als de
bot verbonden is. Handig voor monitoring; de Docker-image gebruikt het ook als
healthcheck.

Zet poort 8688 **niet** open naar internet: alleen Jellyseerr en je eigen
diensten hoeven erbij.

## Ontwikkelen

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-aiohttp
.venv/bin/python -m pytest
```

De tests draaien zonder Discord en zonder echte Jellyseerr. Ze gebruiken een
nep-API en bootsen de placeholder-vervanging van Jellyseerr na.

## Releases

Elke push naar `main` wordt getest en als `:main` naar ghcr.io gezet. Een
release maak je door een versietag te pushen:

```bash
git tag -a v1.1.0 -m "v1.1.0"
git push origin v1.1.0
```

GitHub Actions draait dan de tests, bouwt de image (`:1.1.0`, `:1.1`, `:1` en
`:latest`) en maakt een release op GitHub met de commits sinds de vorige versie
en het `docker pull`-commando. Een tag als `v1.1.0-rc.1` wordt een pre-release
en verandert `:latest` niet.

Versienummers volgen [SemVer](https://semver.org/lang/nl/): **patch**
(`1.0.1`) voor bugfixes, **minor** (`1.1.0`) voor nieuwe functies, **major**
(`2.0.0`) als je bij het updaten iets moet aanpassen, zoals een hernoemde
omgevingsvariabele.
