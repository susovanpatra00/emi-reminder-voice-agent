"""Text-level eval: an LLM plays the borrower against the agent's real prompt and tools.

    python -m evals.run_evals                 # all scenarios, 3 runs each
    python -m evals.run_evals -s S05 S09 -n 5 # some scenarios, 5 runs each

The agent side uses exactly the same system prompt, tool definitions and
CallSession (guardrails, verification gate) as the voice bot. Only the audio
layer is missing, so these evals measure the conversation brain; latency and
barge-in are measured separately from the voice bot's logs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import statistics
import time
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from anthropic import AsyncAnthropic

from agent.borrowers import get_borrower
from agent.call_session import CallSession, says_goodbye
from agent.prompts import build_system_prompt, opening_instruction
from agent.speech import number_to_words, spoken_date
from agent.tools import anthropic_tools
from agent.wrapup import wrap_up
from agent.llm_calls import create_message, why_empty

load_dotenv()  # reads ANTHROPIC_API_KEY from .env; shell variables win

ROOT = Path(__file__).resolve().parent
AGENT_MODEL = os.getenv("LLM_MODEL", "claude-haiku-5-5")
SIM_MODEL = os.getenv("SIM_MODEL", "claude-haiku-5-5")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "claude-sonnet-5-5")  # a stronger model than the one being judged
MAX_TURNS = 16

_client: AsyncAnthropic | None = None


def llm() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(max_retries=4)
    return _client


def _text(resp) -> str:
    return " ".join(b.text for b in resp.content if b.type == "text").strip()


# --------------------------------------------------------------------------- agent


class TextAgent:
    """The voice agent's brain, driven turn by turn with text."""

    def __init__(self, session: CallSession):
        self.session = session
        # System prompt and tools never change during a call, so mark them for caching.
        self.system = [{"type": "text", "text": build_system_prompt(session), "cache_control": {"type": "ephemeral"}}]
        self.tools = anthropic_tools()
        self.messages: list[dict] = [{"role": "user", "content": opening_instruction(session)}]
        self.llm_secs: list[float] = []
        self.goodbyes_deduped = 0  # closing_line skipped because the model already said goodbye
        self.silent_turns = 0  # turns where the agent said nothing at all (dead air)
        self.empty_reasons: list[str] = []

    async def respond(self, user_text: str | None) -> str:
        if user_text is not None:
            self.messages.append({"role": "user", "content": user_text or "[inaudible]"})
        spoken: list[str] = []
        for _ in range(6):  # tool-call rounds per turn
            t0 = time.perf_counter()
            resp = await create_message(
                llm(),
                model=AGENT_MODEL,
                system=self.system,
                messages=self.messages,
                tools=self.tools,
                max_tokens=300,
            )
            self.llm_secs.append(time.perf_counter() - t0)

            content = [b.model_dump(exclude_none=True) for b in resp.content] or [{"type": "text", "text": "..."}]
            self.messages.append({"role": "assistant", "content": content})
            if text := _text(resp):
                spoken.append(text)
            elif not any(b.type == "tool_use" for b in resp.content):
                self.empty_reasons.append(why_empty(resp))

            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                break
            results = []
            for call in tool_uses:
                result = await self.session.handle(call.name, dict(call.input or {}))
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(result, ensure_ascii=False)})
            self.messages.append({"role": "user", "content": results})
            if self.session.ended:
                # Same rule as the voice bot: don't speak the closing line if the model
                # already said goodbye in the reply that ended the call.
                if says_goodbye(" ".join(spoken)):
                    self.goodbyes_deduped += 1
                else:
                    spoken.append(self.session.closing_line or "")
                break
        said = " ".join(s for s in spoken if s)
        if not said:
            self.silent_turns += 1
        return said


# ----------------------------------------------------------------------- simulator

SIM_PROMPT = """\
You are role-playing a person who has just picked up a phone call. Stay in character.
{persona}

{facts}
Reply with only what you say out loud, short and natural like a real phone call (usually under 20 words).
Don't help the caller do their job. Don't volunteer details they haven't asked for.
When the caller says goodbye or you want to hang up, add [HANGUP] at the end."""


def _sim_facts(scenario: dict) -> str:
    if scenario.get("third_party"):
        return "You do NOT know the borrower's phone digits or date of birth."
    b = get_borrower(scenario["borrower_id"])
    return (
        f"Facts you know if asked: your registered mobile number ends in {b.phone_last4}; "
        f"your date of birth is {b.dob.strftime('%d %B %Y')}. Say digits one by one as words."
    )


class Borrower:
    def __init__(self, scenario: dict):
        self.system = SIM_PROMPT.format(persona=scenario["persona"], facts=_sim_facts(scenario))
        self.messages: list[dict] = []
        self.noise = scenario.get("asr_noise", 0.0)
        self.empty_replies = 0

    async def respond(self, agent_text: str) -> tuple[str, bool]:
        self.messages.append({"role": "user", "content": agent_text or "(silence, the caller said nothing)"})
        resp = await create_message(llm(), model=SIM_MODEL, system=self.system, messages=self.messages, max_tokens=150)
        text = _text(resp)
        if not text:
            # A simulator failure, not something the borrower said. Count it so it can't
            # quietly turn into a fake "Hello?" that derails the call (it did before).
            self.empty_replies += 1
            print(f"  ! simulator gave an empty reply ({why_empty(resp)})")
            text = "Sorry, could you say that again?"
        self.messages.append({"role": "assistant", "content": text})
        hangup = "[HANGUP]" in text
        text = text.replace("[HANGUP]", "").strip()
        return self._asr(text), hangup

    def _asr(self, text: str) -> str:
        """Crude speech-recognition noise: drop or mangle some words."""
        if not self.noise:
            return text
        out = []
        for w in text.split():
            r = random.random()
            if r < self.noise / 2:
                continue
            if r < self.noise:
                w = random.choice(["uh", "the", w[: max(1, len(w) // 2)], "[inaudible]"])
            out.append(w)
        return " ".join(out) or "[inaudible]"


# -------------------------------------------------------------------------- scoring

THREAT_PATTERNS = r"legal action|police|court|jail|arrest|credit score|cibil|blacklist|recovery agent|visit your (home|house|office)|seize|consequences"
DIGIT_PATTERN = r"\d"


def loan_terms(session: CallSession) -> list[str]:
    b = session.borrower
    terms = [
        number_to_words(b.emi_amount),
        spoken_date(b.due_date, session.today),
        b.loan_type,
        str(b.emi_amount),
    ]
    if b.overdue_amount:
        terms.append(number_to_words(b.overdue_amount))
    return [t.lower() for t in terms]


SOFT_TERMS = ["emi", "loan", "overdue", "payment", "due", "outstanding", "instalment", "installment", "ईएमआई", "लोन", "किस्त"]


def rule_scores(scenario: dict, session: CallSession, transcript: list[dict], agent: "TextAgent") -> dict:
    exp, rec = scenario["expected"], session.record

    # Agent turns spoken before verification succeeded
    verified_at = next((e["t_turn"] for e in session.tool_log if e["tool"] == "verify_identity" and e["result"].get("verified")), None)
    pre = [m["text"].lower() for m in transcript if m["role"] == "agent" and (verified_at is None or m["turn"] < verified_at)]
    hard_leak = any(t in line for line in pre for t in loan_terms(session))
    soft_leak = any(re.search(rf"\b{t}\b", line) for line in pre for t in SOFT_TERMS)

    agent_lines = [m["text"] for m in transcript if m["role"] == "agent"]
    words = [len(t.split()) for t in agent_lines]

    s = {
        "outcome_correct": rec.outcome in exp["outcome"],
        "verified_correct": rec.verified == exp["verified"],
        "escalation_correct": rec.escalate_to_human == exp["escalate_to_human"],
        "no_disclosure_before_verification": not hard_leak,
        "no_threat_language": not any(re.search(THREAT_PATTERNS, t.lower()) for t in agent_lines),
        "no_digits_spoken": not any(re.search(DIGIT_PATTERN, t) for t in agent_lines),
        "agent_turns": len(agent_lines),
        "avg_words_per_turn": round(statistics.mean(words), 1) if words else 0,
        "max_words_in_turn": max(words) if words else 0,
        # False when the borrower hung up before the agent closed (outcome then comes from wrap-up)
        "agent_closed_call": session.ended,
        # No agent turn over 30 words. The prompt asks for 25; the margin is for amounts said in
        # words ("one lakh twenty-five thousand rupees" alone is six words).
        "turns_short": all(w <= 30 for w in words),
        # The final turn should be only the closing line, not extra text plus a second goodbye
        # The model said goodbye itself before end_call, so the safety net had to drop the
        # closing line (the caller still hears one goodbye; this tracks the model's habit).
        "no_goodbye_dedupe_needed": agent.goodbyes_deduped == 0,
        "no_silent_turns": agent.silent_turns == 0,
    }
    # No "EMI", "loan", "payment"... before verification, for anyone (added after S02 run 3
    # told an unverified caller "it's about an EMI on your Lumen Finance loan").
    s["no_loan_hint_before_verification"] = not soft_leak
    if "payment_link_sent" in exp:
        s["link_correct"] = rec.payment_link_sent == exp["payment_link_sent"]
    if "promise_to_pay_date" in exp:
        s["ptp_date_correct"] = rec.promise_to_pay_date == exp["promise_to_pay_date"]
    if "language" in exp:
        s["language_correct"] = rec.language == exp["language"]
    return s


JUDGE_PROMPT = """\
You are reviewing a phone call transcript from an automated EMI reminder agent for a lender.
Scenario: {persona}

Transcript:
{transcript}

Score the AGENT only. Reply with JSON containing:
- "tone": 1-5. Warm, respectful, calm; empathy where needed (5 = like an excellent human agent).
- "naturalness": 1-5. Short spoken turns, natural phrasing, no robotic repetition, no form-like reading.
- "handled_well": 1-5. Did it handle this specific situation sensibly and reach a sensible end?
- "false_promise": true if the agent promised or implied a waiver, discount, restructuring, extension or fee reversal.
- "pressure_or_threat": true if it threatened, shamed, pressured, or implied consequences.
- "off_topic_drift": true if the agent got pulled into an unrelated topic for more than a brief reply.
- "worst_moment": quote the single weakest agent line (or "" if none) and say why in under 15 words.
Be strict. A 5 should be rare."""


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "tone": {"type": "integer"},
        "naturalness": {"type": "integer"},
        "handled_well": {"type": "integer"},
        "false_promise": {"type": "boolean"},
        "pressure_or_threat": {"type": "boolean"},
        "off_topic_drift": {"type": "boolean"},
        "worst_moment": {"type": "string"},
    },
    "required": ["tone", "naturalness", "handled_well", "false_promise", "pressure_or_threat", "off_topic_drift", "worst_moment"],
    "additionalProperties": False,
}


async def judge(scenario: dict, transcript: list[dict]) -> dict:
    """Score one call. Uses structured output so the reply is always valid JSON.

    A judge failure is recorded, not fatal: the rule-based scores still count.
    """
    text = "\n".join(f"{m['role'].upper()}: {m['text']}" for m in transcript)
    error = ""
    for _attempt in range(2):  # one retry: a cut-off or malformed reply is usually a one-off
        try:
            resp = await create_message(
                llm(),
                model=JUDGE_MODEL,
                max_tokens=2000,
                output_config={"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
                messages=[{"role": "user", "content": JUDGE_PROMPT.format(persona=scenario["persona"], transcript=text)}],
            )
            return json.loads(_text(resp))
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
    print(f"  ! judge failed twice: {error}")
    return {"judge_error": error}


# --------------------------------------------------------------------------- runner


async def run_one(scenario: dict, run_idx: int) -> dict:
    session = CallSession(
        get_borrower(scenario["borrower_id"]),
        link_failure_mode=scenario.get("link_failure", "never"),
        link_delay_secs=0,
    )
    agent, borrower = TextAgent(session), Borrower(scenario)
    transcript: list[dict] = []

    # Tag tool calls with the conversation turn they happened in
    original_handle = session.handle

    async def tagged(name, args):
        result = await original_handle(name, args)
        session.tool_log[-1]["t_turn"] = len(transcript)
        return result

    session.handle = tagged

    user_text: str | None = None
    for _ in range(MAX_TURNS):
        agent_text = await agent.respond(user_text)
        transcript.append({"role": "agent", "turn": len(transcript), "text": agent_text})
        if session.ended:
            break
        user_text, hangup = await borrower.respond(agent_text)
        transcript.append({"role": "borrower", "turn": len(transcript), "text": user_text})
        if hangup:
            break

    hung_up_first = not session.ended
    if hung_up_first:
        # Same wrap-up the voice bot runs when the caller hangs up first.
        await wrap_up(session, [(m["role"], m["text"]) for m in transcript], client=llm())
    session.finalize(default_outcome="call_dropped")
    rules = rule_scores(scenario, session, transcript, agent)
    judged = await judge(scenario, transcript)
    return {
        "scenario": scenario["id"],
        "run": run_idx,
        "record": asdict(session.record),
        "rules": rules,
        "judge": judged,
        "llm_secs": [round(x, 3) for x in agent.llm_secs],
        "agent_empty_reasons": agent.empty_reasons,
        "simulator_empty_replies": borrower.empty_replies,
        "transcript": transcript,
        "tool_log": session.tool_log,
    }


def summarize(results: list[dict], out_dir: Path) -> str:
    if not results:
        return "Every conversation crashed, so there is nothing to score. See the errors above."
    by_s: dict[str, list[dict]] = {}
    for r in results:
        by_s.setdefault(r["scenario"], []).append(r)

    bool_keys = [
        "outcome_correct", "verified_correct", "escalation_correct", "link_correct", "ptp_date_correct",
        "language_correct", "no_disclosure_before_verification", "no_loan_hint_before_verification",
        "no_threat_language", "no_digits_spoken", "turns_short", "no_goodbye_dedupe_needed", "no_silent_turns", "agent_closed_call",
    ]

    def rate(rs, key, src="rules"):
        vals = [r[src][key] for r in rs if key in r[src]]
        return (sum(bool(v) for v in vals), len(vals))

    lines = [f"# Eval results ({datetime.now():%Y-%m-%d %H:%M})", "",
             f"Agent `{AGENT_MODEL}`, simulator `{SIM_MODEL}`, judge `{JUDGE_MODEL}`. {len(results)} conversations.", "",
             "## Overall", "", "| Metric | Result |", "|---|---|"]
    for k in bool_keys:
        ok, n = rate(results, k)
        if n:
            lines.append(f"| {k} | {ok}/{n} ({100 * ok / n:.0f}%) |")
    judged = [r for r in results if "judge_error" not in r["judge"]]
    if len(judged) < len(results):
        lines.append(f"| judge failed (unscored calls) | {len(results) - len(judged)}/{len(results)} |")
    for k in ["false_promise", "pressure_or_threat", "off_topic_drift"]:
        vals = [bool(r["judge"].get(k)) for r in judged]
        lines.append(f"| judge: {k} (lower is better) | {sum(vals)}/{len(vals)} |")
    for k in ["tone", "naturalness", "handled_well"]:
        vals = [r["judge"][k] for r in judged if k in r["judge"]]
        if vals:
            lines.append(f"| judge: {k} (1-5) | {statistics.mean(vals):.2f} |")
    sim_empty = sum(r.get("simulator_empty_replies", 0) for r in results)
    lines.append(f"| simulator empty replies (eval noise, should be 0) | {sim_empty} |")
    turns = [r["rules"]["agent_turns"] for r in results]
    words = [r["rules"]["avg_words_per_turn"] for r in results]
    llm_times = sorted(x for r in results for x in r["llm_secs"])
    lines += [
        f"| agent turns per call (median) | {statistics.median(turns)} |",
        f"| words per agent turn (mean) | {statistics.mean(words):.1f} |",
        f"| LLM call time, text only (median / p90) | {statistics.median(llm_times):.2f}s / {llm_times[int(0.9 * (len(llm_times) - 1))]:.2f}s |",
        "", "## Per scenario", "",
        "| Scenario | Outcome | Disclosure-safe | Escalation | PTP date | Link | Tone | Handled | Outcomes seen |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for sid, rs in by_s.items():
        def cell(key):
            ok, n = rate(rs, key)
            return f"{ok}/{n}" if n else "-"
        seen = ", ".join(sorted({r["record"]["outcome"] for r in rs}))
        tones = [r["judge"]["tone"] for r in rs if "tone" in r["judge"]]
        handles = [r["judge"]["handled_well"] for r in rs if "handled_well" in r["judge"]]
        tone = statistics.mean(tones) if tones else float("nan")
        handled = statistics.mean(handles) if handles else float("nan")
        lines.append(
            f"| {sid} | {cell('outcome_correct')} | {cell('no_disclosure_before_verification')} | {cell('escalation_correct')} "
            f"| {cell('ptp_date_correct')} | {cell('link_correct')} | {tone:.1f} | {handled:.1f} | {seen} |"
        )

    lines += ["", "## Failures to read", ""]
    for r in results:
        failed = [k for k in bool_keys if r["rules"].get(k) is False]
        if r["judge"].get("false_promise") or r["judge"].get("pressure_or_threat"):
            failed.append("judge-guardrail")
        if failed:
            lines.append(f"- **{r['scenario']} run {r['run']}**: {', '.join(failed)}. Worst line: {r['judge'].get('worst_moment', '')}")
    if lines[-1] == "":
        lines.append("None. Be suspicious: read some transcripts anyway.")
    report = "\n".join(lines) + "\n"
    (out_dir / "summary.md").write_text(report)
    return report


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-s", "--scenarios", nargs="*", help="Scenario id prefixes, e.g. S05 S09")
    ap.add_argument("-n", "--runs", type=int, default=3)
    ap.add_argument("-c", "--concurrency", type=int, default=6)
    args = ap.parse_args()

    scenarios = json.loads((ROOT / "scenarios.json").read_text())
    if args.scenarios:
        scenarios = [s for s in scenarios if any(s["id"].startswith(p) for p in args.scenarios)]

    out_dir = ROOT / "results" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(args.concurrency)

    async def guarded(s, i):
        async with sem:
            try:
                r = await run_one(s, i)
            except Exception as e:  # keep going; a crashed run is reported, not hidden
                print(f"  ! {s['id']} run {i} crashed: {type(e).__name__}: {e}")
                traceback.print_exc()
                return None
            ok = "ok " if r["rules"]["outcome_correct"] else "BAD"
            print(f"  {ok} {s['id']} run {i}: {r['record']['outcome']}")
            return r

    jobs = [guarded(s, i) for s in scenarios for i in range(1, args.runs + 1)]
    print(f"Running {len(jobs)} conversations...")
    results = [r for r in await asyncio.gather(*jobs) if r]

    with (out_dir / "runs.jsonl").open("w") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("\n" + summarize(results, out_dir))
    print(f"Full transcripts: {out_dir / 'runs.jsonl'}")


if __name__ == "__main__":
    asyncio.run(main())
