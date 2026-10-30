"""Tool definitions, written once and converted for Pipecat and for the OpenAI eval loop."""

from __future__ import annotations

from .call_session import OUTCOMES

TOOLS: list[dict] = [
    {
        "name": "verify_identity",
        "description": (
            "Check the caller's identity. Call this as soon as they give the last four digits of their "
            "registered mobile number, or their date of birth. Loan details are returned only on success."
        ),
        "properties": {
            "method": {"type": "string", "enum": ["phone_last4", "dob"]},
            "value": {
                "type": "string",
                "description": "For phone_last4: exactly the digits they said, e.g. '4821' or 'four eight two one'. "
                "For dob: the date as YYYY-MM-DD.",
            },
        },
        "required": ["method", "value"],
    },
    {
        "name": "send_payment_link",
        "description": "Send a payment link to the borrower's registered number. Only after they agree to receive it.",
        "properties": {"channel": {"type": "string", "enum": ["sms", "whatsapp"]}},
        "required": [],
    },
    {
        "name": "record_promise_to_pay",
        "description": "Record the date the borrower commits to paying, when it is not the due date itself.",
        "properties": {"date_iso": {"type": "string", "description": "YYYY-MM-DD, worked out from the calendar."}},
        "required": ["date_iso"],
    },
    {
        "name": "schedule_callback",
        "description": "Record when the person asked to be called back.",
        "properties": {"when": {"type": "string", "description": "As they said it, plus a date if clear, e.g. 'tomorrow evening (2026-10-31)'."}},
        "required": ["when"],
    },
    {
        "name": "flag_for_human",
        "description": "Flag the account for a human colleague: financial hardship, a dispute, or when they ask for a person.",
        "properties": {
            "reason": {"type": "string", "enum": ["hardship", "dispute", "requested_human", "other"]},
            "details": {"type": "string", "description": "One short line of what they said."},
        },
        "required": ["reason"],
    },
    {
        "name": "set_language",
        "description": "Switch the language you speak in. Call it as soon as the caller switches or asks.",
        "properties": {"language": {"type": "string", "enum": ["english", "hindi"]}},
        "required": ["language"],
    },
    {
        "name": "end_call",
        "description": (
            "End the call. The closing_line is spoken and then the line is hung up. Call it in a reply "
            "with no other text, so the goodbye is said only once. Use it as the last action of every call."
        ),
        "properties": {
            "outcome": {"type": "string", "enum": OUTCOMES},
            "closing_line": {"type": "string", "description": "Everything left to say, ending with a short warm goodbye, in the current language. Under 25 words."},
            "notes": {"type": "string", "description": "One line for the collections team."},
        },
        "required": ["outcome", "closing_line"],
    },
]


def openai_tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": {"type": "object", "properties": t["properties"], "required": t["required"]},
            },
        }
        for t in TOOLS
    ]


def anthropic_tools() -> list[dict]:
    tools = [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": {"type": "object", "properties": t["properties"], "required": t["required"]},
        }
        for t in TOOLS
    ]
    tools[-1]["cache_control"] = {"type": "ephemeral"}  # cache all tool definitions
    return tools


def pipecat_tools():
    from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
    from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema

    return ToolsSchema(
        standard_tools=[
            FunctionSchema(name=t["name"], description=t["description"], properties=t["properties"], required=t["required"])
            for t in TOOLS
        ]
    )
