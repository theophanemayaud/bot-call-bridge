"""Warm handoff policy: hold for a person, then relay a second SIP leg to the owner.

The bridge mixes the two legs itself. SIP REFER would drop this process off the
call (no owner context, no retry). An OVH conference room is a separate product.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from call_bridge.config import Settings

OnUnavailable = Literal["tell", "hangup"]


def clean_dial(value: str) -> str:
    return value.strip().replace(" ", "").replace("-", "").replace(".", "")


class HandoffPolicy(BaseModel):
    """SCRIPT opt-in. Omit the object and the call stays single-leg.

    `to` is optional. When it is empty the bridge uses `HANDOFF_OWNER_NUMBER`.
    """

    to: str | None = None
    owner_name: str | None = Field(
        default=None,
        description="Spoken name. Never a phone number. Defaults to HANDOFF_OWNER_NAME.",
    )
    party: str | None = Field(
        default=None,
        description="Who the owner is being connected to, e.g. 'the support agent'.",
    )
    connect_line: str | None = Field(
        default=None,
        description="Verbatim line played to the human before the owner is dialed.",
    )
    context: str | None = Field(
        default=None,
        description="Verbatim line played only to the owner before the legs are relayed.",
    )
    lead_seconds: float | None = Field(default=None, ge=0, le=180)
    max_hold_seconds: float | None = Field(default=None, ge=0, le=3600)
    owner_ring_seconds: float | None = Field(default=None, ge=0.05, le=120)
    owner_retries: int | None = Field(default=None, ge=0, le=3)
    on_unavailable: OnUnavailable | None = None

    @field_validator("to")
    @classmethod
    def clean_to(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = clean_dial(value)
        if not cleaned:
            return None
        if not any(ch.isdigit() for ch in cleaned):
            raise ValueError("handoff.to must be a dialable number")
        return cleaned


@dataclass(frozen=True, slots=True)
class ResolvedHandoff:
    to: str
    owner_name: str
    party: str
    connect_line: str
    context: str
    park_line: str
    apology_tell: str
    apology_hangup: str
    lead_seconds: float
    max_hold_seconds: float
    owner_ring_seconds: float
    owner_retries: int
    on_unavailable: OnUnavailable
    language: str


def _primary_lang(language: str) -> str:
    return (language or "en").lower().split("-", 1)[0].split("_", 1)[0]


def _lines(language: str, owner_name: str, party: str) -> dict[str, str]:
    if _primary_lang(language) == "fr":
        return {
            "connect_line": f"Un instant, je vous mets en relation avec {owner_name}.",
            "context": f"{party} est en ligne.",
            "park_line": f"Merci de patienter, j'attends encore {party}.",
            "apology_tell": (
                f"Je suis désolé, {owner_name} ne décroche pas. Je continue avec vous."
            ),
            "apology_hangup": (
                f"Je suis désolé, {owner_name} ne décroche pas. Je vais raccrocher."
            ),
        }
    return {
        "connect_line": f"One moment, I'm connecting you with {owner_name}.",
        "context": f"The {party} is on the line.",
        "park_line": f"Please stay on the line. I'm still waiting for the {party}.",
        "apology_tell": f"I'm sorry, {owner_name} didn't pick up. I'll keep going from here.",
        "apology_hangup": f"I'm sorry, {owner_name} didn't pick up. I'll hang up now.",
    }


def resolve_handoff(
    spec: HandoffPolicy,
    settings: Settings | None,
    *,
    language: str = "en",
) -> ResolvedHandoff:
    """Merge the SCRIPT object with env defaults. `to` may be empty here."""
    french = _primary_lang(language) == "fr"
    owner_name = (spec.owner_name or "").strip()
    if not owner_name and settings is not None:
        owner_name = settings.handoff_owner_name.strip()
    if not owner_name:
        owner_name = "le propriétaire" if french else "the owner"
    party = (spec.party or "").strip() or ("l'agent" if french else "agent")
    defaults = _lines(language, owner_name, party)
    number = spec.to or ""
    if not number and settings is not None:
        number = clean_dial(settings.handoff_owner_number)
    if number and not any(ch.isdigit() for ch in number):
        raise ValueError("owner number must contain digits")
    lead = spec.lead_seconds
    if lead is None:
        lead = settings.handoff_lead_seconds if settings is not None else 0.0
    hold = spec.max_hold_seconds
    if hold is None:
        hold = settings.handoff_max_hold_seconds if settings is not None else 600.0
    ring = spec.owner_ring_seconds
    if ring is None:
        ring = settings.handoff_owner_ring_seconds if settings is not None else 20.0
    retries = spec.owner_retries
    if retries is None:
        retries = settings.handoff_owner_retries if settings is not None else 1
    unavailable = spec.on_unavailable
    if unavailable is None:
        unavailable = settings.handoff_on_unavailable if settings is not None else "tell"
    return ResolvedHandoff(
        to=number,
        owner_name=owner_name,
        party=party,
        connect_line=(spec.connect_line or "").strip() or defaults["connect_line"],
        context=(spec.context or "").strip() or defaults["context"],
        park_line=defaults["park_line"],
        apology_tell=defaults["apology_tell"],
        apology_hangup=defaults["apology_hangup"],
        lead_seconds=float(lead),
        max_hold_seconds=float(hold),
        owner_ring_seconds=float(ring),
        owner_retries=int(retries),
        on_unavailable=unavailable,
        language=language,
    )


def handoff_instructions(policy: ResolvedHandoff) -> str:
    if policy.lead_seconds > 0:
        lead = (
            f"- You may call bring_in_owner when you believe a person will pick up "
            f"within {policy.lead_seconds:g} seconds (for example you are next in queue). "
            "Do not call it during a long hold.\n"
        )
    else:
        lead = "- Do not call bring_in_owner before a live person is actually on the line.\n"
    return f"""WARM HANDOFF
This call stays up through menus and hold music until a real person answers. The bridge then calls {policy.owner_name} and connects the two people. You do not transfer the call yourself.
The bridge will tell that person: {policy.connect_line}
The bridge will tell only {policy.owner_name}, before connecting them: {policy.context}

This overrides the hold-music hangup rule. Do not hang up because you hear hold music, a queue message, or an IVR menu. Those are not voicemail. Leave a voicemail only when you are sure it is a mailbox (a beep after a personal greeting), then hang up.

- If an IVR menu asks for digits and you know which ones, call dial_digits with only those characters (0-9, *, #). Do not say the digits aloud. Then keep waiting.
- Call human_answered only when a live person has started a real conversation (they greet you, ask a question, or say their name). Do not call it for a recording, hold music, or a queue announcement.
{lead}- After human_answered, stay silent. The bridge speaks the connecting line and calls {policy.owner_name}.
- Once the two people are connected you are muted. Do not speak, and do not hang up unless the SCRIPT says the call is over.
"""
