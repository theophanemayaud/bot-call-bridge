from __future__ import annotations

from typing import Literal

from call_bridge.voice.provider import ToolSpec

# Injected while ask_orchestrator is in flight. Context for the model, not a
# line it must read aloud.
ORCHESTRATOR_CHECKING_CONTEXT = (
    "The orchestrator is checking this. It may take a few seconds. "
    "You can let the person on the line know if you want, for example that you are looking it up. "
    "Do not invent the fact. Do not call a tool on this turn."
)

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
        "Pass that question in the question argument. The callee stays on the line. "
        "The orchestrator is checking and may take a few seconds. You can let the person "
        "know if you want. Do not invent the fact. Do not use this after a spoken goodbye "
        "or when you intend to hang up."
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


def default_call_tools() -> list[ToolSpec]:
    return [HANGUP_TOOL, ASK_ORCHESTRATOR_TOOL]


# Local tools run in this process (hangup → SIP BYE after queued goodbye audio).
# dial_digits (DTMF) would be added here: a ToolSpec in default_call_tools(), the
# name in this set, and a branch in the session dispatcher. Do not route it to
# the orchestrator.
LOCAL_TOOL_NAMES = frozenset({"hangup"})

# The Call orchestrator answers these on the events WebSocket via /tool-results.
ORCHESTRATOR_TOOL_NAMES = frozenset({"ask_orchestrator"})


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
