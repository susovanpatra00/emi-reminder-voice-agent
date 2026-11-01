# Lumen Finance: EMI reminder voice agent

A voice agent that calls a borrower about an upcoming or overdue EMI, verifies who it is talking
to, reminds them clearly, finds out what they intend to do, offers a payment link, and records a
structured outcome. The call runs in the browser; no phone number is needed.

**Demo video (4 min):** https://drive.google.com/file/d/1a12b3w-geQpL1OzXnkKGkBQ06Z42OD-Z/view?usp=drive_link — normal call, hardship call, Hindi call, and the outcome record

## Run it

```bash
uv venv && source .venv/bin/activate
uv pip install -r requirements.txt
cp .env.example .env          # add ANTHROPIC_API_KEY, DEEPGRAM_API_KEY, CARTESIA_API_KEY, CARTESIA_VOICE_ID

BORROWER_ID=B002 python main.py -t webrtc
```

Open http://localhost:7860/client, click **Connect**, allow the microphone. The agent opens the
call. When the call ends, the outcome record, transcript and per-turn latency are saved to
`outcomes/`. The borrowers are in `data/borrowers.json` (B001 to B008); verification uses the last
four digits of the registered number or the date of birth.

```bash
python -m pytest tests                   # offline unit tests of the guardrail logic
python -m evals.run_evals -n 3           # all scenarios, 3 runs each (uses Anthropic)
python -m evals.run_evals -s S05 S09 -n 5
python -m scripts.latency_summary        # voice latency over all saved calls
```

## How the conversation is structured

```
browser mic ─► Silero VAD + smart-turn ─► Deepgram nova-3 (multi) ─► Claude Haiku 5.5 + tools ─► Cartesia sonic ─► browser
                                                                          │
                                                                    CallSession (agent/call_session.py)
                                                                    verification gate, tool logic, outcome record
```

One LLM, one system prompt (`agent/prompts.py`) describing the flow: **open → verify → remind →
intent → link → close**. The model moves through it by calling tools:

| Tool | What it does |
|---|---|
| `verify_identity` | Checks last-4 digits or DOB. Two attempts. Returns the loan details only on success. |
| `send_payment_link` | Mock, takes ~2 s, can be made to fail. Refuses if not verified. |
| `record_promise_to_pay` | Validates the date (not in the past), returns it in spoken form to read back. |
| `schedule_callback`, `flag_for_human` | Record the request in the outcome. |
| `set_language` | Switches the TTS voice language (English ↔ Hindi) mid-call. |
| `end_call` | Writes the outcome, speaks the closing line and hangs up. |

Outcome record (saved per call):
`{borrower_id, verified, outcome, promise_to_pay_date, payment_link_sent, escalate_to_human, escalation_reason, callback_time, language, notes, duration_secs}`

## Key design decisions

**Guardrails in code, not only in the prompt.** The system prompt does not contain the amount,
due date or loan type. The model only learns them from a successful `verify_identity` result, so
it cannot leak them to a family member or a wrong number, however the conversation goes. Loan
tools refuse until verification passes, verification locks after two failures, and `hardship` or
`dispute` outcomes always set `escalate_to_human` even if the model forgets to flag.

**Numbers and dates are pre-spoken.** `agent/speech.py` turns `12500` into "twelve thousand five
hundred rupees" (Indian lakh/crore system) and `2026-11-05` into "the fifth of November". The LLM
never sees digits, so the TTS never reads "1-2-5-0-0". A small calendar in the prompt lets the
model turn "next Friday" into a real date; the tool reads it back as "Friday, the sixth of November".

**No dead air on tools.** When `send_payment_link` starts, a short filler line ("Sure, sending
that to you now.") is spoken immediately while the slow call runs.

**Turn-taking and barge-in** use Pipecat defaults: Silero VAD plus the local smart-turn model to
decide when the caller has finished (so a pause mid-sentence doesn't trigger a reply), and
interruptions enabled, so the agent stops as soon as the caller speaks. Silence: after 7 s the
agent checks if they can hear, then repeats the question, then politely ends the call
(`no_response`).

**Language.** Deepgram runs in `multi` mode, which follows English/Hindi code-mixing. When the
caller switches, the model calls `set_language` and the Cartesia voice changes language.

## Tech choices and trade-offs

- **Pipecat** handles the audio plumbing (WebRTC, VAD, interruptions, streaming between stages),
  so the work goes into the conversation.
- **Deepgram nova-3**: fast streaming STT with Hindi/English code-switching.
- **Cartesia sonic**: low time-to-first-audio and supports Hindi. Voice choice matters a lot for
  how natural it sounds; pick an Indian-accent voice.
- **Claude Haiku 5.5**: fast, cheap, reliable tool calling. The system prompt and tools are
  cached, since they're identical on every turn. A bigger model may follow the rules slightly
  better but adds latency on every turn.

## Latency

Measured with Pipecat's `UserBotLatencyObserver`: time from the caller stopping speaking to the
first agent audio, per turn, saved with each call and summarised by `scripts.latency_summary`.
Debug logs show a per-turn breakdown (endpointing wait, STT, LLM, TTS).

Measured over 22 turns in 4 browser calls with the final code: **median 1.54s, p90 1.87s,
max 4.13s**. A typical turn is about 0.5s speech recognition, 0.75s to the LLM's first token
and 0.1–0.25s to first audio, plus the wait to be sure the caller has finished.

On turns where a tool runs first (verify, record a promise, flag for a colleague), the
agent says a short "One moment" straight away. Before that change these turns left
4–7 seconds of silence (p90 5.11s). The time to the actual answer on those turns is
unchanged; removing the second LLM call would be the real fix.

What reduces it: everything streams (STT → LLM tokens → TTS sentence by sentence), short replies,
a small fast model, pre-formatted facts so the model writes less, and fillers that cover tool time.

## Evals

`evals/scenarios.json` has 17 scenarios: the required borrower responses plus tricky ones (a
husband pressing for details, someone failing verification and still asking for the amount, a
waiver demand, a noisy line, the payment-link tool failing).

`evals/run_evals.py` plays each scenario several times: an LLM plays the borrower against the
agent's real prompt, tools and `CallSession`. Scoring:

| Metric | How | Trust |
|---|---|---|
| Outcome, verified, escalation, link, PTP date, language | Exact match with the expected record | High |
| Disclosure before verification | Searches pre-verification agent lines for the amount, date, loan type | High for leaks of those facts |
| Third party: no hint of a loan | Searches for words like EMI, loan, payment | Medium (may flag harmless phrasing) |
| Threat language, digits spoken, words per turn, turns per call | Rules | High, but narrow |
| Tone, naturalness, handled well, false promise, pressure, off-topic | LLM judge (Claude Sonnet 5.5, strict rubric, forced structured output) | Medium: read the transcripts it flags |

Limits: the evals are text-level, so they test the conversation brain, not audio, latency or
barge-in. The simulated borrower is more cooperative and articulate than real people. The judge is
from the same model family as the agent, which may make it more lenient.

See `REPORT.md` for results and failures found.
