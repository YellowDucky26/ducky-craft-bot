import io
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

from duckybot.cogs.inbox import MAX_UPLOAD_BYTES, Inbox, forward_embed, forward_reason
from duckybot.config import Config

ME = SimpleNamespace(id=1000, bot=True)
TARGET = 2000
BOT_ROLE = SimpleNamespace(id=55)


def attachment(name, size=100):
    async def to_file():
        return discord.File(io.BytesIO(b"x" * 10), filename=name)

    return SimpleNamespace(filename=name, size=size, url=f"https://cdn.example/{name}", to_file=to_file)


def message(*, guild=True, mentions=(), role_mentions=(), author_id=42, bot=False, content="Hoi Kwakkel", attachments=()):
    author = SimpleNamespace(
        id=author_id, bot=bot, name="kwak", display_name="Kwak", mention=f"<@{author_id}>",
        display_avatar=SimpleNamespace(url="https://cdn.example/a.png"),
    )
    return SimpleNamespace(
        guild=SimpleNamespace(id=1, self_role=BOT_ROLE) if guild else None,
        channel=SimpleNamespace(name="algemeen"),
        mentions=list(mentions), role_mentions=list(role_mentions),
        author=author, content=content, attachments=list(attachments), stickers=[],
        created_at=datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc),
        jump_url="https://discord.com/channels/1/2/3",
    )


class FakeBot:
    def __init__(self, forward_user_id=TARGET):
        self.config = Config(
            discord_token="x", guild_id=1, jellyseerr_url="http://seerr", jellyseerr_api_key="k",
            webhook_secret="s", forward_user_id=forward_user_id,
        )
        self.user = ME
        self.dms: list[tuple[int, dict]] = []

    async def dm(self, user_id, **kwargs):
        self.dms.append((user_id, kwargs))
        return True


def test_forward_reason():
    assert forward_reason(message(guild=False), ME) == "dm"
    assert forward_reason(message(mentions=[ME]), ME) == "ping"
    assert forward_reason(message(role_mentions=[BOT_ROLE]), ME) == "ping"
    assert forward_reason(message(), ME) is None
    assert forward_reason(message(mentions=[SimpleNamespace(id=7)]), ME) is None


def test_forward_embed():
    embed = forward_embed(message(mentions=[ME]), "ping", set())
    assert embed.title == "🔔 Ping in #algemeen"
    assert embed.description == "Hoi Kwakkel"
    assert embed.author.name == "Kwak (@kwak)" and embed.footer.text == "ID 42"

    msg = message(guild=False, content="", attachments=[attachment("klein.png"), attachment("groot.mkv")])
    embed = forward_embed(msg, "dm", {"klein.png"})
    assert embed.title == "📨 DM aan Kwakkel" and embed.description == "*(geen tekst zichtbaar)*"
    assert [f.value for f in embed.fields] == ["[groot.mkv](https://cdn.example/groot.mkv)"]


async def test_dm_is_forwarded_with_files():
    bot = FakeBot()
    big = attachment("groot.mkv", size=MAX_UPLOAD_BYTES)
    await Inbox(bot).on_message(message(guild=False, attachments=[attachment("foto.png"), big]))
    [(user_id, kwargs)] = bot.dms
    assert user_id == TARGET
    assert kwargs["content"] == "<@42>" and kwargs["view"] is None  # een DM heeft geen bruikbare link
    assert [f.filename for f in kwargs["files"]] == ["foto.png"]
    assert "groot.mkv" in kwargs["embed"].fields[0].value


async def test_ping_is_forwarded_with_jump_button():
    bot = FakeBot()
    await Inbox(bot).on_message(message(mentions=[ME]))
    [(_, kwargs)] = bot.dms
    assert kwargs["view"].children[0].url == "https://discord.com/channels/1/2/3"


@pytest.mark.parametrize("msg, forward_user_id", [
    (message(), TARGET),                                    # gewoon kanaalbericht
    (message(guild=False, bot=True), TARGET),               # andere bot
    (message(guild=False, author_id=TARGET), TARGET),       # de ontvanger zelf
    (message(guild=False), 0),                              # doorsturen staat uit
])
async def test_not_forwarded(msg, forward_user_id):
    bot = FakeBot(forward_user_id)
    await Inbox(bot).on_message(msg)
    assert bot.dms == []
