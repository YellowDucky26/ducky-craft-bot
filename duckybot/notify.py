"""Vertaalt Jellyseerr-webhookpayloads naar Discord-meldingen (zonder Discord-I/O, zodat het testbaar is)."""
import re
from dataclasses import dataclass, field
from typing import Any

COLOR_PENDING = 0xFFA500
COLOR_APPROVED = 0x3498DB
COLOR_AVAILABLE = 0x57F287
COLOR_DECLINED = 0xED4245
COLOR_ISSUE = 0x9B59B6
COLOR_TEST = 0xF5C518

PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")
SNOWFLAKE_RE = re.compile(r"\d{15,21}")

# type -> (kop, kleur, doelen, DM-tekst)
# Doelen: "admin" = adminkanaal, "public" = aanvraagkanaal, "dm" = DM aan de aanvrager (of bij issues de melder).
# In de DM-tekst kunnen {title}, {commenter} en {comment} staan.
EVENTS: dict[str, tuple[str, int, set[str], str | None]] = {
    "MEDIA_PENDING": ("📨 Nieuwe aanvraag", COLOR_PENDING, {"admin"}, None),
    "MEDIA_AUTO_REQUESTED": ("🤖 Automatische aanvraag", COLOR_PENDING, {"admin"}, None),
    "MEDIA_AUTO_APPROVED": ("✅ Aangevraagd", COLOR_APPROVED, {"public"}, None),
    "MEDIA_APPROVED": ("✅ Aanvraag goedgekeurd", COLOR_APPROVED, {"public", "dm"},
                       "Je aanvraag voor **{title}** is goedgekeurd! Je hoort het zodra hij klaarstaat."),
    "MEDIA_AVAILABLE": ("🍿 Nu beschikbaar", COLOR_AVAILABLE, {"public", "dm"},
                        "**{title}** staat klaar in Jellyfin. Veel kijkplezier! 🦆"),
    "MEDIA_DECLINED": ("❌ Aanvraag afgewezen", COLOR_DECLINED, {"admin", "dm"},
                       "Je aanvraag voor **{title}** is helaas afgewezen."),
    "MEDIA_FAILED": ("⚠️ Aanvraag mislukt", COLOR_DECLINED, {"admin"}, None),
    "ISSUE_CREATED": ("🐞 Nieuw probleem gemeld", COLOR_ISSUE, {"admin"}, None),
    "ISSUE_COMMENT": ("💬 Reactie op probleem", COLOR_ISSUE, {"admin", "dm"},
                      "💬 **{commenter}** reageerde op je melding over **{title}**:\n{comment}"),
    "ISSUE_RESOLVED": ("✔️ Probleem opgelost", COLOR_AVAILABLE, {"admin", "dm"},
                       "Het probleem dat je meldde bij **{title}** is opgelost. Bedankt voor het melden! 🛠️"),
    "ISSUE_REOPENED": ("🔁 Probleem heropend", COLOR_ISSUE, {"admin", "dm"},
                       "🔁 Je melding over **{title}** is heropend; er wordt opnieuw naar gekeken."),
    "TEST_NOTIFICATION": ("🦆 Testmelding van Jellyseerr", COLOR_TEST, {"admin"}, None),
}


def clean(value: Any) -> str:
    """Lege string voor ontbrekende waarden en niet-vervangen ``{{placeholders}}``."""
    if value is None:
        return ""
    return PLACEHOLDER_RE.sub("", str(value)).strip()


def snowflakes(*values: Any) -> list[int]:
    ids: list[int] = []
    for value in values:
        for match in SNOWFLAKE_RE.findall(clean(value)):
            if int(match) not in ids:
                ids.append(int(match))
    return ids


@dataclass
class Notice:
    notification_type: str
    heading: str
    title: str
    description: str
    color: int
    targets: set[str]
    dm_text: str | None = None
    image: str = ""
    request_id: int | None = None
    requester: str = ""
    requester_discord_ids: list[int] = field(default_factory=list)
    reporter: str = ""  # bij issues: wie het probleem meldde
    reporter_discord_ids: list[int] = field(default_factory=list)
    issue_id: int | None = None
    commenter: str = ""  # bij reacties: wie er reageerde (die krijgt zelf geen DM)
    commenter_discord_ids: list[int] = field(default_factory=list)
    media_type: str = ""
    tmdb_id: int | None = None
    jellyfin_item_id: str = ""
    extra: list[tuple[str, str]] = field(default_factory=list)


def parse_seerr_payload(payload: dict[str, Any]) -> Notice:
    ntype = clean(payload.get("notification_type")).upper() or "UNKNOWN"
    heading, color, targets, dm = EVENTS.get(ntype, (clean(payload.get("event")) or ntype, COLOR_TEST, {"admin"}, None))

    media = payload.get("media") or {}
    request = payload.get("request") or {}
    issue = payload.get("issue") or {}
    comment = payload.get("comment") or {}

    title = clean(payload.get("subject")) or "Onbekende titel"
    description = clean(payload.get("message"))
    if len(description) > 500:
        description = description[:497] + "..."

    request_id = clean(request.get("request_id"))
    issue_id = clean(issue.get("issue_id"))
    commenter = clean(comment.get("commentedBy_username"))
    comment_text = clean(comment.get("comment_message"))
    if len(comment_text) > 500:
        comment_text = comment_text[:497] + "..."
    tmdb_id = clean(media.get("tmdbId"))

    extra: list[tuple[str, str]] = []
    for item in payload.get("extra") or []:
        if isinstance(item, dict) and clean(item.get("name")) and clean(item.get("value")):
            extra.append((clean(item["name"]), clean(item["value"])))
    if issue:
        if reporter := clean(issue.get("reportedBy_username")):
            extra.append(("Gemeld door", reporter))
        if issue_type := clean(issue.get("issue_type")):
            extra.append(("Soort", issue_type))
    if comment and (msg := clean(comment.get("comment_message"))):
        extra.append((f"Reactie van {clean(comment.get('commentedBy_username')) or '?'}", msg[:1000]))

    return Notice(
        notification_type=ntype,
        heading=heading,
        title=title,
        description=description,
        color=color,
        targets=set(targets),
        dm_text=dm.format(
            title=title,
            commenter=commenter or "Iemand",
            comment="\n".join(f"> {line}" for line in comment_text.splitlines()) or "> (geen tekst)",
        ) if dm else None,
        image=clean(payload.get("image")),
        request_id=int(request_id) if request_id.isdigit() else None,
        requester=clean(request.get("requestedBy_username")),
        requester_discord_ids=snowflakes(
            request.get("requestedBy_settings_discordIds"), request.get("requestedBy_settings_discordId")
        ),
        reporter=clean(issue.get("reportedBy_username")),
        reporter_discord_ids=snowflakes(
            issue.get("reportedBy_settings_discordIds"), issue.get("reportedBy_settings_discordId")
        ),
        issue_id=int(issue_id) if issue_id.isdigit() else None,
        commenter=commenter,
        commenter_discord_ids=snowflakes(
            comment.get("commentedBy_settings_discordIds"), comment.get("commentedBy_settings_discordId")
        ),
        media_type=clean(media.get("media_type")),
        tmdb_id=int(tmdb_id) if tmdb_id.isdigit() else None,
        jellyfin_item_id=clean(media.get("jellyfinMediaId")),
        extra=extra,
    )
