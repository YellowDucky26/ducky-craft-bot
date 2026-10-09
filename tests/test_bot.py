import json
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

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
    }, "posted": {}}

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
    app.router.add_post("/api/v1/request", create_request)
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


# --- opslag ----------------------------------------------------------------------

def test_link_store_roundtrip(tmp_path):
    store = LinkStore(str(tmp_path))
    store.set(111, 2, "Kwak")
    assert LinkStore(str(tmp_path)).get(111) == {"seerr_id": 2, "name": "Kwak"}
    assert store.discord_ids_for(name="kwak") == [111]
    assert store.discord_ids_for(seerr_id=2) == [111]
    assert store.remove(111) and not store.remove(111)


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
    from duckybot.cogs.media import RequestView

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
    assert view.seasons == [2] and view.request_button.label == "Ontbrekende seizoenen aanvragen (1)"
