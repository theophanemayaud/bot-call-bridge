from __future__ import annotations

from typing import Literal

from call_bridge.voice.provider import ToolSpec

HANGUP_TOOL = ToolSpec(
    name="hangup",
    description=(
        "End the phone call AFTER you have already spoken an audible goodbye on the line "
        "(e.g. in French: « Je te laisse, au revoir ! » / « Je raccroche, au revoir ! »). "
        "Do not ask for permission to hang up unless the SCRIPT says so — but always say "
        "you are hanging up / say goodbye first, then call this tool. Also use after a "
        "voicemail message or when the callee asks to stop. The bridge sends SIP BYE. "
        "Do not call ask_orchestrator when the conversation is already over."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Short machine-readable reason, e.g. completed, voicemail, refused, error.",
            },
            "summary": {
                "type": "string",
                "description": "One-sentence outcome for the orchestrator.",
            },
        },
        "required": ["reason"],
    },
)

ASK_ORCHESTRATOR_TOOL = ToolSpec(
    name="ask_orchestrator",
    description=(
        "Ask the Call/orchestrator a clarifying question when you need a fact you do not have. "
        "The callee stays on the line. Keep the question short. Do not use this after a "
        "spoken goodbye or when you intend to hang up."
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question for the orchestrator, not the callee.",
            },
            "context": {
                "type": "string",
                "description": "What the callee just said that created the gap.",
            },
            "urgency": {
                "type": "string",
                "enum": ["low", "normal", "high"],
            },
        },
        "required": ["question"],
    },
)


HUMAN_ANSWERED_TOOL = ToolSpec(
    name="human_answered",
    description=(
        "Call this only when a live person has just started talking on the target line. "
        "Not for hold music, IVR menus, queue recordings, or voicemail. The bridge tells "
        "them it is connecting the owner, dials the owner, and then mutes you. Stay silent "
        "after this tool."
    ),
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One line: who answered and what they just said.",
            },
            "party": {
                "type": "string",
                "description": "Who picked up, e.g. support agent. Optional.",
            },
        },
        "required": ["summary"],
    },
)

BRING_IN_OWNER_TOOL = ToolSpec(
    name="bring_in_owner",
    description=(
        "Start dialing the owner now. Use when a live person is already on the line, or, "
        "only if the prompt allows a lead, shortly before you expect that person. "
        "Do not use it for an IVR menu or a long hold. Stay silent after this tool."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "human_ready or lead.",
            },
        },
    },
)

DIAL_DIGITS_TOOL = ToolSpec(
    name="dial_digits",
    description=(
        "Send DTMF digits to the target line (IVR / extension). Pass only 0-9, *, #. "
        "Do not speak the digits. Then keep listening."
    ),
    parameters={
        "type": "object",
        "properties": {
            "digits": {
                "type": "string",
                "description": "Digits to send, e.g. 1 or 12#.",
            },
        },
        "required": ["digits"],
    },
)


def default_call_tools() -> list[ToolSpec]:
    return [HANGUP_TOOL, ASK_ORCHESTRATOR_TOOL]


def handoff_call_tools() -> list[ToolSpec]:
    """Tools for a SCRIPT that opted into warm handoff. Single-leg calls omit these."""
    return [
        HANGUP_TOOL,
        ASK_ORCHESTRATOR_TOOL,
        HUMAN_ANSWERED_TOOL,
        BRING_IN_OWNER_TOOL,
        DIAL_DIGITS_TOOL,
    ]


# Local tools run in this process. ask_orchestrator is the only orchestrator tool.
# PRs #12 and #13 add the same split with LOCAL_TOOL_NAMES = {hangup}. On merge,
# keep the union below so human_answered / bring_in_owner / dial_digits stay local.
LOCAL_TOOL_NAMES = frozenset({"hangup", "human_answered", "bring_in_owner", "dial_digits"})
ORCHESTRATOR_TOOL_NAMES = frozenset({"ask_orchestrator"})
HANDOFF_TOOL_NAMES = frozenset({"human_answered", "bring_in_owner", "dial_digits"})


def tool_route(name: str) -> Literal["local", "orchestrator", "unknown"]:
    if name in LOCAL_TOOL_NAMES:
        return "local"
    if name in ORCHESTRATOR_TOOL_NAMES:
        return "orchestrator"
    return "unknown"


def tools_as_openai_functions(tools: list[ToolSpec]) -> list[dict]:
    """Shared function-tool shape used by Grok Voice Realtime."""
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        for tool in tools
    ]
