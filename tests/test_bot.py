import json
import os
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import discord
import pytest
from aiohttp import web

from duckybot.bot import presence_text
from duckybot.cogs.accounts import Accounts, find_inactive, top
from duckybot.cogs.invites import Invites
from duckybot.cogs.library import Library, describe_session, digest_due, group_new_items
from duckybot.cogs.media import EMAIL_RE, Media
from duckybot.cogs.notifications import Notifications, parse_color
from duckybot.config import Config
from duckybot.jellyseerr import Jellyseerr, discord_ids_of, requestable_seasons
from duckybot.notify import parse_seerr_payload
from duckybot.storage import LinkStore
from duckybot.web import create_app

TEMPLATE = json.loads((Path(__file__).parent.parent / "jellyseerr-webhook.json").read_text())

# Alleen sleutels die Seerr kent; de oude `discordId`-variant blijft dus letterlijk staan.
SEERR_VALUES = {
    "notification_type": "MEDIA_AVAILABLE",
    "event": "Movie Request Now Available",
    "subject": "Dune (2021)",
    "message": "Een jonge edelman ...",
    "image": "https://image.tmdb.org/t/p/w600_and_h900_bestv2/dune.jpg",
    "media_type": "movie",
    "media_tmdbid": "438631",
    "media_status": "AVAILABLE",
    "media_jellyfinMediaId": "abc123",
    "request_id": "42",
    "requestedBy_username": "Kwak",
    "requestedBy_settings_discordIds": "123456789012345678,223456789012345678",
}


def render_like_seerr(template, values, *, has_request=True, has_issue=False):
    """Benadert parseKeys() uit Seerr's webhook-agent."""
    out = {}
    present = {"{{media}}": True, "{{request}}": has_request, "{{issue}}": has_issue, "{{comment}}": False}
    for key, value in template.items():
        if key == "{{extra}}":
            out["extra"] = []
            continue
        if key in present:
            out[key.strip("{}")] = render_like_seerr(value, values) if present[key] else None
            continue
        if isinstance(value, str):
            for name, replacement in values.items():
                value = value.replace("{{" + name + "}}", replacement, 1)
        elif isinstance(value, dict):
            value = render_like_seerr(value, values)
        out[key] = value
    return out


def make_config(tmp_path, **overrides):
    base = dict(
        discord_token="x", guild_id=1, jellyseerr_url="http://seerr", jellyseerr_api_key="k",
        jellyseerr_public_url="https://seerr.example", jellyfin_public_url="https://jf.example",
        admin_channel_id=10, request_channel_id=20, announce_channel_id=30,
        webhook_secret="geheim", data_dir=str(tmp_path),
    )
    base.update(overrides)
    return Config(**base)


class FakeBot:
    def __init__(self, config):
        self.config = config
        self.links = LinkStore(config.data_dir)
        self.sent: list[tuple[int, dict]] = []
        self.dms: list[tuple[int, dict]] = []
        self.cog = None

    async def send_to(self, channel_id, **kwargs):
        self.sent.append((channel_id, kwargs))
        return object()

    async def dm(self, user_id, **kwargs):
        self.dms.append((user_id, kwargs))
        return True

    def get_cog(self, name):
        return self.cog

    def is_ready(self):
        return True


# --- notify ------------------------------------------------------------------

def test_template_is_valid_and_parses():
    payload = render_like_seerr(TEMPLATE, SEERR_VALUES)
    notice = parse_seerr_payload(payload)
    assert notice.notification_type == "MEDIA_AVAILABLE"
    assert notice.title == "Dune (2021)"
    assert notice.request_id == 42
    assert notice.tmdb_id == 438631
    assert notice.jellyfin_item_id == "abc123"
    assert notice.requester == "Kwak"
    assert notice.requester_discord_ids == [123456789012345678, 223456789012345678]
    assert notice.targets == {"public", "dm"}
    assert "Dune (2021)" in notice.dm_text


def test_legacy_jellyseerr_discord_id_and_missing_request():
    values = {**SEERR_VALUES, "notification_type": "TEST_NOTIFICATION"}
    del values["requestedBy_settings_discordIds"]
    values["requestedBy_settings_discordId"] = "323456789012345678"
    notice = parse_seerr_payload(render_like_seerr(TEMPLATE, values))
    assert notice.requester_discord_ids == [323456789012345678]

    notice = parse_seerr_payload(render_like_seerr(TEMPLATE, values, has_request=False))
    assert notice.request_id is None and notice.requester_discord_ids == []
    assert notice.targets == {"admin"}


def test_unknown_type_goes_to_admin():
    notice = parse_seerr_payload({"notification_type": "SOMETHING_NEW", "event": "Iets", "subject": "X"})
    assert notice.targets == {"admin"} and notice.heading == "Iets"


def test_parse_color():
    assert parse_color("#3498db") == 0x3498DB
    assert parse_color(255) == 255
    assert parse_color("geen kleur") == parse_color(None)


# --- jellyseerr helpers --------------------------------------------------------

def test_discord_ids_of_both_formats():
    assert discord_ids_of({"discordIds": ["1", " 2 "]}) == ["1", "2"]
    assert discord_ids_of({"discordId": "3"}) == ["3"]
    assert discord_ids_of({"discordIds": None, "discordId": ""}) == []


def test_requestable_seasons_skips_available_requested_and_specials():
    tv = {
        "seasons": [{"seasonNumber": n} for n in range(0, 5)],
        "mediaInfo": {
            "seasons": [{"seasonNumber": 1, "status": 5}],
            "requests": [
                {"status": 1, "seasons": [{"seasonNumber": 2}]},
                {"status": 3, "seasons": [{"seasonNumber": 3}]},  # afgewezen: opnieuw aanvraagbaar
            ],
        },
    }
    assert requestable_seasons(tv) == [3, 4]
    assert requestable_seasons({"seasons": [{"seasonNumber": 1}]}) == [1]


# --- jellyseerr client tegen nep-server ------------------------------------------

@pytest.fixture
async def fake_seerr(aiohttp_server):
    state = {"requests": [], "settings": {
        1: {"discordIds": [], "discordId": None},
        2: {"discordIds": ["999"]},
        3: {"discordId": "777"},  # oude Jellyseerr
    }, "posted": {}, "main": {2: {"username": "kwak", "email": "kwak@jellyfin", "locale": "nl"}}, "issues": []}

    def check_key(request):
        if request.headers.get("X-Api-Key") != "k":
            raise web.HTTPForbidden()

    async def search(request):
        check_key(request)
        return web.json_response({"results": [
            {"id": 1, "mediaType": "movie", "title": "Dune"},
            {"id": 2, "mediaType": "person", "name": "Iemand"},
            {"id": 3, "mediaType": "tv", "name": "Dune: Prophecy"},
        ]})

    async def users(request):
        check_key(request)
        skip = int(request.query["skip"])
        all_users = [{"id": i, "displayName": f"user{i}"} for i in (1, 2, 3)]
        return web.json_response({"pageInfo": {"results": 3}, "results": all_users[skip:skip + 2]})

    async def settings(request):
        check_key(request)
        uid = int(request.match_info["id"])
        if request.method == "POST":
            state["posted"][uid] = await request.json()
            return web.json_response(state["posted"][uid])
        return web.json_response(state["settings"][uid])

    async def main_settings(request):
        check_key(request)
        uid = int(request.match_info["id"])
        if request.method == "POST":
            state["main"][uid] = await request.json()
        return web.json_response(state["main"][uid])

    async def create_issue(request):
        check_key(request)
        state["issues"].append(await request.json())
        return web.json_response({"id": len(state["issues"])}, status=201)

    async def trending(request):
        check_key(request)
        return await search(request)

    async def upcoming_tv(request):
        check_key(request)
        return web.json_response({"results": [{"id": 9, "name": "Andor", "firstAirDate": "2026-11-20"}]})

    async def request_list(request):
        check_key(request)
        skip = int(request.query["skip"])
        dates = ["2026-10-10", "2026-10-05", "2026-10-02", "2026-09-20", "2026-09-01"]
        rows = [{"id": 10 - i, "createdAt": f"{d}T12:00:00.000Z"} for i, d in enumerate(dates)]
        return web.json_response({"pageInfo": {"results": len(rows)}, "results": rows[skip:skip + 2]})

    async def create_request(request):
        check_key(request)
        body = await request.json()
        state["requests"].append((request.headers.get("X-Api-User"), body))
        if body["mediaId"] == 404:
            return web.json_response({"message": "Quota bereikt"}, status=403)
        return web.json_response({"id": 7, "status": 1}, status=201)

    async def import_jf(request):
        check_key(request)
        return web.json_response([], status=201)

    app = web.Application()
    app.router.add_get("/api/v1/search", search)
    app.router.add_get("/api/v1/user", users)
    app.router.add_route("*", "/api/v1/user/{id}/settings/notifications", settings)
    app.router.add_route("*", "/api/v1/user/{id}/settings/main", main_settings)
    app.router.add_post("/api/v1/request", create_request)
    app.router.add_get("/api/v1/request", request_list)
    app.router.add_post("/api/v1/issue", create_issue)
    app.router.add_get("/api/v1/discover/trending", trending)
    app.router.add_get("/api/v1/discover/tv/upcoming", upcoming_tv)
    app.router.add_post("/api/v1/user/import-from-jellyfin", import_jf)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        yield Jellyseerr(session, str(server.make_url("")).rstrip("/"), "k"), state


async def test_search_filters_people(fake_seerr):
    seerr, _ = fake_seerr
    assert [r["id"] for r in await seerr.search("dune")] == [1, 3]


async def test_request_as_user_with_seasons(fake_seerr):
    seerr, state = fake_seerr
    await seerr.create_request(5, "tv", 3, [1, 2])
    await seerr.create_request(5, "movie", 1)
    assert state["requests"][0] == ("5", {"mediaType": "tv", "mediaId": 3, "seasons": [1, 2]})
    assert state["requests"][1] == ("5", {"mediaType": "movie", "mediaId": 1})


async def test_request_error_message(fake_seerr):
    from duckybot.jellyseerr import JellyseerrError

    seerr, _ = fake_seerr
    with pytest.raises(JellyseerrError) as exc:
        await seerr.create_request(5, "movie", 404)
    assert exc.value.status == 403 and exc.value.message == "Quota bereikt"


async def test_find_user_by_discord_id_paginates_and_supports_legacy(fake_seerr):
    seerr, _ = fake_seerr
    assert (await seerr.find_user_by_discord_id("999"))["id"] == 2
    assert (await seerr.find_user_by_discord_id("777"))["id"] == 3
    assert await seerr.find_user_by_discord_id("123") is None


async def test_set_discord_id_keeps_other_settings(fake_seerr):
    seerr, state = fake_seerr
    await seerr.set_discord_id(2, "555")
    assert state["posted"][2]["discordIds"] == ["555"] and state["posted"][2]["discordId"] == "555"


async def test_set_email_keeps_other_settings(fake_seerr):
    seerr, state = fake_seerr
    await seerr.set_email(2, "kwak@example.nl")
    assert state["main"][2] == {"username": "kwak", "email": "kwak@example.nl", "locale": "nl"}


def test_email_validation():
    assert EMAIL_RE.match("kwak@example.nl")
    for bad in ("kwak", "kwak@", "kwak@example", "kw ak@example.nl", "a@b@c.nl"):
        assert not EMAIL_RE.match(bad), bad


# --- slash-commando's -------------------------------------------------------------

def test_slash_commands_are_english(tmp_path):
    bot = FakeBot(make_config(tmp_path))
    tree = discord.app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    commands = {}
    # Invites is een GroupCog: de cog zelf is de groep /invite.
    groups = [Invites(bot).app_command]
    singles = [cog(bot).get_app_commands() for cog in (Media, Notifications, Library, Accounts)]
    for command in [*groups, *(c for cs in singles for c in cs)]:
        payload = command.to_dict(tree)
        options = payload.get("options", [])
        if options and options[0]["type"] == 1:  # subcommando's, zoals /invite discord
            for sub in options:
                commands[f"{payload['name']} {sub['name']}"] = [o["name"] for o in sub.get("options", [])]
        else:
            commands[payload["name"]] = [o["name"] for o in options]
    assert commands == {
        "request": ["title"],
        "trending": ["type"],
        "upcoming": ["type"],
        "issue": ["title", "type", "description", "season", "episode"],
        "now-playing": [],
        "whats-new": ["days"],
        "reset-password": ["member"],
        "inactive": [],
        "stats": [],
        "my-requests": [],
        "my-account": [],
        "link": ["member", "user"],
        "unlink": ["member"],
        "invite discord": ["max_uses", "expires"],
        "invite jellyfin": ["member", "username"],
        "announce": ["channel", "ping"],
    }


def test_presence_text():
    assert presence_text(1) == "Kijkt naar 1 online lid"
    assert presence_text(12) == "Kijkt naar 12 online leden"


# --- opslag ----------------------------------------------------------------------

def test_link_store_roundtrip(tmp_path):
    store = LinkStore(str(tmp_path))
    store.set(111, 2, "Kwak")
    assert LinkStore(str(tmp_path)).get(111) == {"seerr_id": 2, "name": "Kwak"}
    assert store.discord_ids_for(name="kwak") == [111]
    assert store.discord_ids_for(seerr_id=2) == [111]
    assert store.remove(111) and not store.remove(111)


def test_link_store_check_writable(tmp_path):
    LinkStore(str(tmp_path)).check_writable()
    assert list(tmp_path.iterdir()) == []

    readonly = tmp_path / "ro"
    readonly.mkdir(mode=0o555)
    if os.access(readonly, os.W_OK):
        pytest.skip("draait als root; schrijfrechten worden niet afgedwongen")
    with pytest.raises(SystemExit, match="Kan niet schrijven"):
        LinkStore(str(readonly)).check_writable()


# --- notificatieroutering ------------------------------------------------------------

async def test_pending_request_gets_buttons_in_admin_channel(tmp_path):
    bot = FakeBot(make_config(tmp_path))
    cog = Notifications(bot)
    payload = render_like_seerr(TEMPLATE, {**SEERR_VALUES, "notification_type": "MEDIA_PENDING"})
    await cog.handle_seerr_webhook(payload)

    assert len(bot.sent) == 1 and not bot.dms
    channel, kwargs = bot.sent[0]
    assert channel == 10
    ids = [item.custom_id for item in kwargs["view"].children]
    assert ids == ["seerr:approve:42", "seerr:decline:42"]
    assert kwargs["embed"].url == "https://seerr.example/movie/438631"


async def test_available_goes_public_and_dms_requester_once(tmp_path):
    bot = FakeBot(make_config(tmp_path))
    bot.links.set(123456789012345678, 2, "Kwak")  # zelfde persoon als in payload: geen dubbele DM
    bot.links.set(444444444444444444, 2, "Kwak")  # tweede gekoppeld Discord-account
    cog = Notifications(bot)
    await cog.handle_seerr_webhook(render_like_seerr(TEMPLATE, SEERR_VALUES))

    assert [c for c, _ in bot.sent] == [20]
    button = bot.sent[0][1]["view"].children[0]
    assert button.url == "https://jf.example/web/#/details?id=abc123"
    assert sorted(u for u, _ in bot.dms) == [123456789012345678, 223456789012345678, 444444444444444444]
    assert "content" not in bot.sent[0][1]  # geen ping in het kanaal; de aanvrager krijgt een DM


async def test_handle_notify_defaults_and_validation(tmp_path):
    bot = FakeBot(make_config(tmp_path))
    cog = Notifications(bot)
    assert await cog.handle_notify({"title": "Server herstart", "color": "#ff0000", "mention_role_id": 5})
    channel, kwargs = bot.sent[0]
    assert channel == 30 and kwargs["content"] == "<@&5>" and kwargs["embed"].color.value == 0xFF0000
    assert await cog.handle_notify({"message": "hoi", "user_id": 9})
    assert bot.dms[0][0] == 9
    with pytest.raises(ValueError):
        await cog.handle_notify({})


# --- web ---------------------------------------------------------------------------

async def test_web_auth_and_routing(tmp_path, aiohttp_client):
    bot = FakeBot(make_config(tmp_path))
    bot.cog = Notifications(bot)
    client = await aiohttp_client(create_app(bot))

    assert (await client.get("/health")).status == 200
    assert (await client.post("/webhook/jellyseerr", json={})).status == 401
    assert (await client.post("/webhook/jellyseerr", json={}, headers={"Authorization": "fout"})).status == 401

    payload = render_like_seerr(TEMPLATE, {**SEERR_VALUES, "notification_type": "TEST_NOTIFICATION"}, has_request=False)
    resp = await client.post("/webhook/jellyseerr", json=payload, headers={"Authorization": "geheim"})
    assert resp.status == 200 and (await resp.json())["type"] == "TEST_NOTIFICATION"

    resp = await client.post("/notify", json={"title": "Hoi"}, headers={"Authorization": "Bearer geheim"})
    assert resp.status == 200
    resp = await client.post("/notify", json={}, headers={"Authorization": "Bearer geheim"})
    assert resp.status == 400
    resp = await client.post("/notify", data="geen json", headers={"Authorization": "geheim"})
    assert resp.status == 400


# --- aanvraagknop ------------------------------------------------------------------

async def test_request_view_button_state(tmp_path):
    from duckybot.cogs.media import RequestView, SeasonSelect

    bot = FakeBot(make_config(tmp_path))

    def movie(status):
        return {"id": 1, "mediaInfo": {"status": status, "jellyfinMediaId": "x"}}

    assert not RequestView(bot, 1, "movie", {"id": 1}).request_button.disabled
    assert not RequestView(bot, 1, "movie", movie(6)).request_button.disabled  # verwijderd: opnieuw aanvragen
    available = RequestView(bot, 1, "movie", movie(5))
    assert available.request_button.disabled and available.request_button.label == "Al beschikbaar"
    assert available.children[-1].url == "https://jf.example/web/#/details?id=x"

    tv = {"id": 2, "seasons": [{"seasonNumber": 1}, {"seasonNumber": 2}],
          "mediaInfo": {"status": 4, "seasons": [{"seasonNumber": 1, "status": 5}, {"seasonNumber": 2, "status": 6}]}}
    view = RequestView(bot, 1, "tv", tv)
    assert view.seasons == [2] and view.request_button.label == "Seizoen 2 aanvragen"
    assert not any(isinstance(c, SeasonSelect) for c in view.children)  # bij één seizoen valt er niets te kiezen

    tv3 = {"id": 3, "seasons": [{"seasonNumber": n} for n in (0, 1, 2, 3)],
           "mediaInfo": {"status": 4, "seasons": [{"seasonNumber": 1, "status": 5}]}}
    view = RequestView(bot, 1, "tv", tv3)
    select = next(c for c in view.children if isinstance(c, SeasonSelect))
    assert [o.value for o in select.options] == ["2", "3"] and select.max_values == 2
    assert view.selected == [2, 3] and view.request_button.label == "Ontbrekende seizoenen aanvragen (2)"
    view.selected = [3]
    view.update_label()
    assert view.request_button.label == "Seizoen 3 aanvragen"


# --- issues -------------------------------------------------------------------------

async def test_issue_resolved_dms_reporter(tmp_path):
    bot = FakeBot(make_config(tmp_path))
    bot.links.set(555555555555555555, 3, "Eend")  # gekoppeld, maar zonder Discord-ID in Jellyseerr
    values = {
        **SEERR_VALUES,
        "notification_type": "ISSUE_RESOLVED",
        "requestedBy_username": "",
        "requestedBy_settings_discordIds": "",
        "issue_type": "Subtitle",
        "reportedBy_username": "Eend",
        "reportedBy_settings_discordIds": "666666666666666666",
    }
    cog = Notifications(bot)
    notice = await cog.handle_seerr_webhook(render_like_seerr(TEMPLATE, values, has_request=False, has_issue=True))
    assert notice.reporter == "Eend" and notice.reporter_discord_ids == [666666666666666666]
    assert sorted(u for u, _ in bot.dms) == [555555555555555555, 666666666666666666]
    assert "opgelost" in bot.dms[0][1]["content"]


async def test_create_issue_and_discover(fake_seerr):
    seerr, state = fake_seerr
    await seerr.create_issue(5, 77, 3, "Ondertitels lopen achter", season=2, episode=4)
    await seerr.create_issue(5, 78, 1, "Beeld hapert")
    assert state["issues"] == [
        {"mediaId": 77, "issueType": 3, "message": "Ondertitels lopen achter", "userId": 5,
         "problemSeason": 2, "problemEpisode": 4},
        {"mediaId": 78, "issueType": 1, "message": "Beeld hapert", "userId": 5},
    ]
    assert [r["id"] for r in await seerr.trending()] == [1, 3]  # personen eruit
    assert [r["mediaType"] for r in await seerr.upcoming("tv")] == ["tv"]


async def test_requests_since_stops_at_older(fake_seerr):
    from datetime import datetime, timezone

    seerr, _ = fake_seerr
    found = await seerr.requests_since(datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert [r["id"] for r in found] == [10, 9, 8]


# --- bibliotheek ----------------------------------------------------------------------

def test_describe_session():
    episode = {
        "UserName": "Kwak", "Client": "Jellyfin Web", "DeviceName": "Firefox",
        "PlayState": {"PositionTicks": 300, "IsPaused": True},
        "NowPlayingItem": {"Type": "Episode", "SeriesName": "Severance", "ParentIndexNumber": 2,
                           "IndexNumber": 3, "Name": "Who Is Alive?", "RunTimeTicks": 1000},
    }
    assert describe_session(episode) == "⏸️ **Kwak** — Severance S02E03 · Who Is Alive? · 30%\n-# Jellyfin Web op Firefox"
    movie = {"UserName": "Eend", "NowPlayingItem": {"Type": "Movie", "Name": "Dune", "ProductionYear": 2021}}
    assert describe_session(movie) == "▶️ **Eend** — Dune (2021)"


def test_group_new_items():
    def ep(season, number, series="s1", name="Severance"):
        return {"Type": "Episode", "Id": f"{series}-{season}-{number}", "SeriesId": series, "SeriesName": name,
                "ParentIndexNumber": season, "IndexNumber": number, "Name": f"Aflevering {number}"}

    items = [ep(2, 5), {"Type": "Movie", "Id": "m1", "Name": "Dune", "ProductionYear": 2021}, ep(2, 4), ep(2, 3),
             ep(2, 2), ep(1, 1, "s2", "Andor")]
    titles = group_new_items(items)
    assert [(t.title, t.item_id) for t in titles] == [("Severance", "s1"), ("Dune", "m1"), ("Andor", "s2")]
    assert titles[0].detail == "📺 4 nieuwe afleveringen: S02E02 t/m S02E05"
    assert titles[1].detail == "🎬 Film · 2021"
    assert titles[2].detail == "📺 Nieuwe aflevering: S01E01 · Aflevering 1"
    assert group_new_items([ep(1, 2), ep(1, 1)])[0].detail == "📺 2 nieuwe afleveringen: S01E01, S01E02"


def test_digest_due():
    from datetime import datetime

    friday, saturday = datetime(2026, 10, 9), datetime(2026, 10, 10)
    assert digest_due(friday, "weekly", 4) and not digest_due(saturday, "weekly", 4)
    assert digest_due(saturday, "daily", 4) and not digest_due(friday, "off", 4)


# --- accounts -------------------------------------------------------------------------

def test_find_inactive_and_parse_date():
    from datetime import datetime, timezone

    from duckybot.jellyfin import parse_date

    assert parse_date("2026-10-11T19:45:31.1234567Z") == datetime(2026, 10, 11, 19, 45, 31, 123456, tzinfo=timezone.utc)
    assert parse_date(None) is None and parse_date("onzin") is None

    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    users = [
        {"Id": "a", "Name": "actief", "LastActivityDate": "2026-10-01T00:00:00Z"},
        {"Id": "b", "Name": "oud", "LastActivityDate": "2026-06-01T00:00:00.0000000Z"},
        {"Id": "c", "Name": "nooit"},
        {"Id": "d", "Name": "admin", "Policy": {"IsAdministrator": True}},
        {"Id": "e", "Name": "uit", "Policy": {"IsDisabled": True}},
    ]
    assert [u["Name"] for u, _ in find_inactive(users, now, 60)] == ["nooit", "oud"]


def test_top():
    from collections import Counter

    assert top(Counter({"a": 3, "b": 5, "c": 0})) == "🥇 **b** — 5\n🥈 **a** — 3"
    assert top(Counter()) == "-"


def test_quota_and_result_label():
    from duckybot.cogs.media import quota_text, result_label

    quota = {"movie": {"limit": 10, "remaining": 3, "days": 7, "used": 7}, "tv": {"used": 2}}
    assert quota_text(quota) == "Films: nog 3 van 10 (per 7 dagen)\nSeries: onbeperkt"
    assert result_label({"mediaType": "movie", "title": "Dune", "releaseDate": "2021-09-15",
                         "mediaInfo": {"status": 5}}) == "🎬 Dune (2021) ✅"
    assert result_label({"mediaType": "tv", "name": "Andor", "firstAirDate": "2026-11-20"}, with_date=True) == \
        "📺 Andor · 20-11-2026"


def test_state_store(tmp_path):
    from duckybot.storage import StateStore

    StateStore(str(tmp_path)).set("digest_last", "2026-10-09T18:00:00+02:00")
    assert StateStore(str(tmp_path)).get("digest_last") == "2026-10-09T18:00:00+02:00"
    assert StateStore(str(tmp_path)).get("onbekend", []) == []


def test_config_schedule_validation(monkeypatch):
    for name, value in {"DISCORD_TOKEN": "x", "GUILD_ID": "1", "JELLYSEERR_URL": "http://s",
                        "JELLYSEERR_API_KEY": "k", "WEBHOOK_SECRET": "g"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DIGEST_DAY", "Zondag")
    cfg = Config.from_env()
    assert cfg.digest == "weekly" and cfg.digest_day == 6 and cfg.report_time == "18:00"

    monkeypatch.setenv("DIGEST", "maandelijks")
    monkeypatch.setenv("REPORT_TIME", "25:00")
    monkeypatch.setenv("TZ", "Europe/Eendenburg")
    with pytest.raises(SystemExit) as exc:
        Config.from_env()
    assert "DIGEST" in str(exc.value) and "REPORT_TIME" in str(exc.value) and "TZ" in str(exc.value)


# --- jellyfin-client tegen nep-server -------------------------------------------------

@pytest.fixture
async def fake_jellyfin(aiohttp_server):
    from duckybot.jellyfin import Jellyfin

    state = {"passwords": [], "policies": {}, "old_api": False}

    async def items(request):
        if request.query.get("Filters") == "IsPlayed":
            return web.json_response({"Items": [], "TotalRecordCount": 12})
        return web.json_response({"Items": [
            {"Id": "3", "Type": "Movie", "DateCreated": "2026-10-10T12:00:00.0000000Z"},
            {"Id": "2", "Type": "Movie", "DateCreated": "2026-10-05T12:00:00.0000000Z"},
            {"Id": "1", "Type": "Movie", "DateCreated": "2026-09-01T12:00:00.0000000Z"},
        ]})

    async def password_new(request):
        if state["old_api"]:
            raise web.HTTPNotFound()
        state["passwords"].append(("new", request.query["userId"], (await request.json())["NewPw"]))
        return web.Response(status=204)

    async def password_old(request):
        state["passwords"].append(("old", request.match_info["id"], (await request.json())["NewPw"]))
        return web.Response(status=204)

    async def user(request):
        return web.json_response({"Id": request.match_info["id"], "Policy": {"IsAdministrator": False, "EnableAllFolders": True}})

    async def policy(request):
        state["policies"][request.match_info["id"]] = await request.json()
        return web.Response(status=204)

    app = web.Application()
    app.router.add_get("/Items", items)
    app.router.add_post("/Users/Password", password_new)
    app.router.add_post("/Users/{id}/Password", password_old)
    app.router.add_get("/Users/{id}", user)
    app.router.add_post("/Users/{id}/Policy", policy)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        yield Jellyfin(session, str(server.make_url("")).rstrip("/"), "k"), state


async def test_jellyfin_items_since_and_played(fake_jellyfin):
    from datetime import datetime, timezone

    jellyfin, _ = fake_jellyfin
    items = await jellyfin.items_added_since(datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert [i["Id"] for i in items] == ["3", "2"]
    assert await jellyfin.played_count("u1") == 12


async def test_jellyfin_password_falls_back_to_old_api(fake_jellyfin):
    jellyfin, state = fake_jellyfin
    await jellyfin.set_password("u1", "geheim1")
    state["old_api"] = True
    await jellyfin.set_password("u2", "geheim2")
    assert state["passwords"] == [("new", "u1", "geheim1"), ("old", "u2", "geheim2")]


async def test_jellyfin_disable_keeps_policy(fake_jellyfin):
    jellyfin, state = fake_jellyfin
    await jellyfin.set_disabled("u1")
    assert state["policies"]["u1"] == {"IsAdministrator": False, "EnableAllFolders": True, "IsDisabled": True}


async def test_invite_jellyfin_is_admin_only(tmp_path):
    bot = FakeBot(make_config(tmp_path))
    bot.is_admin = lambda user: False
    bot.jellyfin = object()  # mag niet gebruikt worden
    sent = []

    async def send_message(text, **kwargs):
        sent.append(text)

    interaction = SimpleNamespace(user=SimpleNamespace(id=1), response=SimpleNamespace(send_message=send_message))
    cog = Invites(bot)
    await cog.jellyfin_invite.callback(cog, interaction, SimpleNamespace(id=2, bot=False), None)
    assert sent == ["Alleen admins kunnen Jellyfin-accounts aanmaken."]


def issue_values(ntype, **extra):
    return {
        **SEERR_VALUES,
        "notification_type": ntype,
        "requestedBy_username": "",
        "requestedBy_settings_discordIds": "",
        "issue_id": "12",
        "issue_type": "Subtitle",
        "reportedBy_username": "Eend",
        "reportedBy_settings_discordIds": "666666666666666666",
        **extra,
    }


def render_issue(values, *, has_comment=False):
    payload = render_like_seerr(TEMPLATE, values, has_request=False, has_issue=True)
    if has_comment:  # render_like_seerr laat {{comment}} standaard weg
        payload["comment"] = render_like_seerr(TEMPLATE["{{comment}}"], values)
    return payload


async def test_issue_comment_dms_reporter_but_not_the_commenter(tmp_path):
    bot = FakeBot(make_config(tmp_path))
    cog = Notifications(bot)

    by_admin = issue_values("ISSUE_COMMENT", comment_message="Opgelost?\nGraag even testen.",
                            commentedBy_username="Admin", commentedBy_settings_discordIds="777777777777777777")
    notice = await cog.handle_seerr_webhook(render_issue(by_admin, has_comment=True))
    assert notice.issue_id == 12 and notice.commenter == "Admin"
    assert [u for u, _ in bot.dms] == [666666666666666666]
    dm = bot.dms[0][1]
    assert dm["content"] == "💬 **Admin** reageerde op je melding over **Dune (2021)**:\n> Opgelost?\n> Graag even testen."
    assert dm["view"].children[0].url == "https://seerr.example/issues/12"

    bot.dms.clear()
    by_reporter = issue_values("ISSUE_COMMENT", comment_message="Nee, nog niet.",
                               commentedBy_username="Eend", commentedBy_settings_discordIds="666666666666666666")
    await cog.handle_seerr_webhook(render_issue(by_reporter, has_comment=True))
    assert bot.dms == []


async def test_issue_reopened_dms_reporter(tmp_path):
    bot = FakeBot(make_config(tmp_path))
    await Notifications(bot).handle_seerr_webhook(render_issue(issue_values("ISSUE_REOPENED")))
    assert [u for u, _ in bot.dms] == [666666666666666666] and "heropend" in bot.dms[0][1]["content"]


async def test_rejected_webhook_is_logged(tmp_path, aiohttp_client, caplog):
    client = await aiohttp_client(create_app(FakeBot(make_config(tmp_path))))
    resp = await client.post("/webhook/jellyseerr", json={}, headers={"Authorization": "fout"})
    assert resp.status == 401
    assert "Geweigerd: POST /webhook/jellyseerr" in caplog.text


async def test_bot_itself_never_gets_dm(tmp_path):
    """Zoals op raptor: de bot per ongeluk gekoppeld aan 'Ruben' en zijn ID in diens Jellyseerr-profiel."""
    bot = FakeBot(make_config(tmp_path))
    bot.user = SimpleNamespace(id=1558926482137419786)
    bot.links.set(770736906187046934, 11, "Ruben")
    bot.links.set(1558926482137419786, 11, "Ruben")
    values = issue_values("ISSUE_RESOLVED", reportedBy_username="Ruben",
                          reportedBy_settings_discordIds="1558926482137419786")
    await Notifications(bot).handle_seerr_webhook(render_issue(values))
    assert [u for u, _ in bot.dms] == [770736906187046934]

