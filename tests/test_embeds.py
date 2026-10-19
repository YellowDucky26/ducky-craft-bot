import json
from types import SimpleNamespace

import discord
import pytest

from duckybot.cogs.embeds import (
    MAX_FIELD_VALUE,
    EmbedError,
    Embeds,
    parse_embed_file,
    parse_message_ref,
)

ME = SimpleNamespace(id=1000, bot=True)

RULES = {
    "content": None,
    "embeds": [
        {
            "title": "Welkom bij Ducky-Craft!",
            "description": "Ducky-Craft is onze **modded Minecraft survival server**.",
            "color": 0x3498DB,
            "fields": [{"name": "Meedoen", "value": "**Serveradres:** `play.kramerhosting.nl`", "inline": False}],
            "footer": {"text": "Het Ducky-Craft Team"},
        },
        {
            "title": "Serverregels Ducky-Craft",
            "url": "https://kramerhosting.nl/minecraft",
            "color": "#27ae60",
            "fields": [{"name": "Algemeen", "value": "**1. Respect voor iedereen**\nGeen lastigvallen."}],
        },
    ],
    "attachments": [],
}


def raw(data) -> bytes:
    return json.dumps(data).encode()


def test_parse_webhook_format():
    content, embeds = parse_embed_file(raw(RULES))
    assert content is None
    assert [e.title for e in embeds] == ["Welkom bij Ducky-Craft!", "Serverregels Ducky-Craft"]
    assert embeds[0].fields[0].name == "Meedoen"
    assert embeds[0].footer.text == "Het Ducky-Craft Team"
    assert embeds[1].color.value == 0x27AE60  # hex-string wordt een getal


def test_parse_other_shapes():
    single = {"title": "Hoi", "description": "Tekst"}
    assert [e.title for e in parse_embed_file(raw(single))[1]] == ["Hoi"]
    assert [e.title for e in parse_embed_file(raw([single, single]))[1]] == ["Hoi", "Hoi"]
    discohook = {"messages": [{"data": {"content": "Boven", "embeds": [single]}}]}
    assert parse_embed_file(raw(discohook))[0] == "Boven"
    assert parse_embed_file(raw({"content": "Alleen tekst"})) == ("Alleen tekst", [])
    # BOM van Windows-editors mag
    assert parse_embed_file(b"\xef\xbb\xbf" + raw(single))[1][0].title == "Hoi"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"{niet json", "Ongeldige JSON op regel 1"),
        (raw("tekst"), "geen JSON-object"),
        (raw({"foo": 1}), "Geen `embeds`"),
        (raw({"embeds": []}), "leeg"),
        (raw({"embeds": [{}]}), "Embed 1 is leeg"),
        (raw({"embeds": [{"title": "x"}] * 11}), "maximum is 10"),
        (raw({"messages": [{"data": {}}, {"data": {}}]}), "2 berichten"),
        (raw({"embeds": [{"title": "x" * 257}]}), "titel is langer"),
        (raw({"embeds": [{"title": "x", "fields": [{"name": "Lang", "value": "x" * (MAX_FIELD_VALUE + 1)}]}]}), "veld “Lang”"),
        (raw({"embeds": [{"title": "x", "fields": [{"name": "", "value": "y"}]}]}), "naam en een waarde"),
        (raw({"content": "x" * 2001}), "berichttekst"),
        (raw({"embeds": [{"description": "x" * 4000}, {"description": "x" * 2001}]}), "6000"),
        (raw({"embeds": [{"description": "Lees <#REGELS-ID> eerst"}]}), "<#REGELS-ID>"),
        (b"\xff\xfe", "geen UTF-8"),
    ],
)
def test_parse_errors(data, message):
    with pytest.raises(EmbedError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_embed_file(data)


def test_real_channel_mention_is_fine():
    _, embeds = parse_embed_file(raw({"embeds": [{"description": "Lees <#123456789012345678> eerst"}]}))
    assert "<#123456789012345678>" in embeds[0].description


def test_parse_message_ref():
    assert parse_message_ref("https://discord.com/channels/1/22/333") == (22, 333)
    assert parse_message_ref("https://ptb.discord.com/channels/1/22/333\n") == (22, 333)
    assert parse_message_ref(" 333 ") == (None, 333)
    with pytest.raises(EmbedError):
        parse_message_ref("hallo")


# --- het commando ----------------------------------------------------------


class FakeResponse:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.deferred = False

    async def defer(self, **kwargs):
        self.deferred = True

    async def send_message(self, text, **kwargs):
        self.sent.append((text, kwargs))


class FakeFollowup:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text, **kwargs):
        self.sent.append(text)


class FakeMessage:
    def __init__(self, author_id=ME.id, channel=None):
        self.author = SimpleNamespace(id=author_id)
        self.jump_url = "https://discord.com/channels/1/22/333"
        self.edited: dict | None = None
        self.channel = channel

    async def edit(self, **kwargs):
        self.edited = kwargs
        return self


class FakeChannel:
    def __init__(self, messages=()):
        self.sent: list[dict] = []
        self.messages = {m_id: m for m_id, m in messages}

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return FakeMessage()

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        return self.messages[message_id]


class FakeBot:
    def __init__(self, admin=True, channels=None):
        self.user = ME
        self.admin = admin
        self.channels = channels or {}

    def is_admin(self, member):
        return self.admin

    async def channel(self, channel_id):
        return self.channels.get(channel_id)


def attachment(data: bytes):
    async def read():
        return data

    return SimpleNamespace(size=len(data), read=read)


def interaction(channel):
    return SimpleNamespace(
        user=SimpleNamespace(id=42, __str__=lambda self: "ruben"),
        channel=channel,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


async def run(cog, inter, **kwargs):
    await Embeds.embed.callback(cog, inter, **kwargs)


async def test_command_posts_in_channel():
    here = FakeChannel()
    inter = interaction(here)
    await run(Embeds(FakeBot()), inter, file=attachment(raw(RULES)), channel=None, ping=None, edit=None)
    assert len(here.sent) == 1
    sent = here.sent[0]
    assert sent["content"] is None
    assert [e.title for e in sent["embeds"]] == ["Welkom bij Ducky-Craft!", "Serverregels Ducky-Craft"]
    assert sent["allowed_mentions"].everyone is False
    assert inter.followup.sent[0].startswith("✅ Bericht geplaatst")


async def test_command_ping_role():
    here = FakeChannel()
    role = SimpleNamespace(mention="<@&7>", is_default=lambda: False, id=7)
    inter = interaction(here)
    await run(Embeds(FakeBot()), inter, file=attachment(raw({"content": "Nieuw!"})), channel=None, ping=role, edit=None)
    assert here.sent[0]["content"] == "<@&7>\nNieuw!"


async def test_command_edits_own_message():
    own = FakeMessage()
    source = FakeChannel(messages=[(333, own)])
    inter = interaction(FakeChannel())
    bot = FakeBot(channels={22: source})
    await run(Embeds(bot), inter, file=attachment(raw(RULES)), channel=None, ping=None,
              edit="https://discord.com/channels/1/22/333")
    assert own.edited is not None and len(own.edited["embeds"]) == 2
    assert inter.followup.sent[0].startswith("✅ Bericht bijgewerkt")


async def test_command_refuses_foreign_message():
    foreign = FakeMessage(author_id=99)
    bot = FakeBot(channels={22: FakeChannel(messages=[(333, foreign)])})
    inter = interaction(FakeChannel())
    await run(Embeds(bot), inter, file=attachment(raw(RULES)), channel=None, ping=None,
              edit="https://discord.com/channels/1/22/333")
    assert foreign.edited is None
    assert "eigen berichten" in inter.followup.sent[0]


async def test_command_reports_bad_file():
    here = FakeChannel()
    inter = interaction(here)
    await run(Embeds(FakeBot()), inter, file=attachment(b"{"), channel=None, ping=None, edit=None)
    assert here.sent == []
    assert inter.followup.sent[0].startswith("❌ Ongeldige JSON")


async def test_command_admin_only():
    here = FakeChannel()
    inter = interaction(here)
    await run(Embeds(FakeBot(admin=False)), inter, file=attachment(raw(RULES)), channel=None, ping=None, edit=None)
    assert here.sent == []
    assert "Alleen admins" in inter.response.sent[0][0]
