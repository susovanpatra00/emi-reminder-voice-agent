# Eval report

## Setup

- **Agent:** `claude-haiku-5-5`, the same prompt, tools and `CallSession` as the voice bot.
- **Simulated borrower:** `claude-haiku-5-5` playing a persona per scenario.
- **Judge:** `claude-sonnet-5-5`, structured output, a strict rubric.
- **Scale:** 18 scenarios × 3 runs = 54 conversations per full run.
- **Scope:** text-level. Latency, barge-in and audio come from voice test calls (see the end).

Full latest summary: [`docs/eval-results.md`](docs/eval-results.md).

## Latest results (54 conversations)

| Metric | Result |
|---|---|
| Outcome correct | 51/54 (94%) |
| Escalation correct | 54/54 (100%) |
| Payment link correct | 50/51 (98%) |
| Promise-to-pay date captured | 8/9 (89%) |
| Language switch (English → Hindi) | 3/3 |
| No loan details before verification | 54/54 (100%) |
| No hint of a loan ("EMI", "payment"…) before verification | 54/54 (100%) |
| No threat language | 54/54 (100%) |
| Judge: false promises / pressure / off-topic drift | 1 / 0 / 0 of 54 |
| Judge: tone · naturalness · handled well (1-5) | 3.59 · 2.98 · 3.19 |
| Every turn ≤ 30 words | 21/54 (39%) |
| Words per agent turn (mean) | 24.1 |
| Agent closed the call itself | 47/54 (87%) |

**Summary:** the guardrails hold across every run, and outcomes are right about 95% of the
time. **Turn length and naturalness are the weakest part** and are not solved (see "Open
issues").

## How the numbers moved

Each row is a full run after one round of fixes.

| Run | Change | Outcome | PTP date | Hindi | False promises | Silent turns | Naturalness | Words/turn |
|---|---|---|---|---|---|---|---|---|
| 1 | First full run (17 scenarios) | 92% | 6/9 | 1/3 | 3 | n/a | 3.00 | 22.0 |
| 2 | Fixes from run 1 (failures 2 and 4 below) | 90% | 9/9 | 1/3 | 1 | 3 calls | 3.08 | 21.6 |
| 3 | Thinking turned off in the harness (failure 3) | 94% | 9/9 | 3/3 | 0 | 0 | 2.94 | 24.1 |
| 4 | Guardrail and narration fixes, +1 scenario (failure 4) | 98% | 9/9 | 3/3 | 1 | 0 | 3.15 | 23.9 |
| 5 | Re-run, no code change | 94% | 8/9 | 3/3 | 1 | 0 | 2.98 | 24.1 |

Runs 4 and 5 use identical code, so **a swing of about ±4 points in outcome accuracy is
noise.** Treat any change smaller than that as unproven.

## Failures found and what I changed

### 1. Long, form-like turns, digits read aloud, and a double goodbye

The very first conversation averaged **26 words per turn**, and its judge naturalness score
was 3/5. The reminder read:
> "Thank you, Rahul, you're verified. This is a reminder that the EMI of twelve thousand five
> hundred rupees on your personal loan ending 7712 is due on the fifth of November."

- **Digits ("7712").** My bug. The tool returned the account ending as "7 7 1 2", and the model
  squeezed it back into digits. **Fix:** every fact is now pre-spoken in words ("seven seven one
  two"), and the account number is only for when the borrower asks which loan. A unit test
  checks that no loan fact contains a digit.
- **Narration** ("you're verified", "before I go into the details"). **Fix:** banned in the
  prompt, and an example call added showing the right length.
- **Double goodbye.** The model wrote a goodbye in the same reply that called `end_call`, and
  then the closing line was spoken too. **Fix:** a code-level safety net in both the bot and the
  harness. If the model already said goodbye in that turn, the closing line is skipped.

**Result:** digits spoken went to 0 in every later run (53/54 in run 5, one slip not yet
investigated). The double goodbye can no longer reach the caller. **Turn length did not
improve.** See "Open issues".

### 2. A fix that caused a regression: the borrower hung up first

After fix 1, the agent confirmed the payment link and then *waited for the borrower to say
bye*. The simulated borrower just hung up, so in **2 of 3 runs the call was recorded as
`call_dropped`**, even though the borrower had clearly said they would pay on time.

**Fix:**
- The agent closes as soon as nothing is left open, putting everything in one closing line
  ("Done, it's on your phone. Thanks, Rahul, have a good day!").
- A post-call **wrap-up** (`agent/wrapup.py`) runs whenever the caller hangs up first. It reads
  the transcript and records the outcome (and a promise date, if one was said but not
  recorded). It keeps what the tools already recorded, and the verification gate still applies,
  so an unverified call can never get a loan outcome.

**Result:** that scenario went from 1/3 to 3/3. The agent closes the call itself in 87% of
calls; the wrap-up covers the rest.

### 3. A bug in the eval harness, not the agent

In runs 1 and 2 the Hindi scenario failed 2 of 3 times. The transcripts showed the agent
repeating "Hi Rahul, I'm still here…" while the borrower said "Hello?" over and over. It looked
like an agent problem.

It wasn't. **"Hello?" was the harness's placeholder for an empty reply from the simulated
borrower.** Current Claude models can think before answering, and with the small token limits
in the harness the thinking used up the whole budget, so no text came back. That also
caused the agent's own silent turns and a judge reply cut off mid-JSON.

**Fix:** all eval, simulator, judge and wrap-up calls now run with thinking turned off (falling
back to the lowest effort if a model refuses). An empty simulator reply is now counted and
reported as eval noise instead of being disguised as speech. The voice bot was not affected,
because Pipecat already sends the lowest thinking setting.

**Result:** Hindi went from 1/3 to 3/3, silent agent turns from 3 calls to 0, and simulator
empty replies to 0.

**Lesson:** the scores pointed at the agent, and the transcripts showed the problem was in the
harness.

### 4. A guardrail slip the eval couldn't see

Reading the "already paid" transcripts after run 3 turned up two problems:
- To a borrower who hadn't verified yet, the agent said: *"It's about an EMI on your Lumen
  Finance loan."* My disclosure check only searched for the **amount, date and loan type**, so
  it scored this call as safe. The "no hint of a loan" check had only been applied to
  third-party scenarios.
- The borrower gave her digits *and* asked a question in the same breath, so the agent answered
  the question and asked for the digits again instead of verifying.

**Fix:**
- The hint check (EMI, loan, payment, overdue…) now applies to every scenario.
- The prompt now says to verify as soon as digits are given, even with a question attached. If
  someone is wary of sharing, explain once, offer the official number, and never hint at the
  topic.
- The simulated borrower in that scenario had drifted into refusing verification, so its persona
  was tightened. That behaviour is realistic, so it became its own scenario,
  `S18_wary_wont_verify`.

**Result:** "already paid" went from 0/3 to 3/3. The hint check passes 54/54 and the new
scenario 3/3.

### Smaller fixes along the way

- **"We won't call you again."** The agent promised this in all 3 stop-calling runs, and the
  judge flagged it as something the agent can't guarantee. It now says it will note the request.
- **Already paid recorded as "dispute".** "Already paid" now means: believe them and close; no
  proof, no flag.
- **Promise date lost** when an irritated borrower hung up right after giving it. The agent now
  records the date in the same reply. Promise-date capture went from 6/9 to 9/9.
- **Tool narration and repetition.** One closing turn said "a colleague will be in touch" three
  times (77 words). The fixes: tools are called silently, and hardship, dispute and callback
  calls now close in a single reply.

## Where I don't trust these evals

- **Text only.** Mishearing, barge-in, silence timing and latency aren't tested here.
- **The simulator drifts.** Without thinking, a simulated borrower changed character (failure
  4). Personas need to be precise, and transcripts need reading.
- **The judge is unreliable on facts.** It claimed details were shared with an unverified caller
  when the tool log shows verification passed first, and it once claimed verification failed
  when it hadn't. I use judge scores to decide which transcripts to read, not as ground truth.
- **The judge and the agent are from the same model family,** which may make the judge
  lenient.
- **Run-to-run noise is about ±4 points** in outcome accuracy (runs 4 and 5).
- **The turn-length check allows 30 words, while the prompt asks for 25.** The extra room is
  because an amount alone can be six words ("one lakh twenty-five thousand rupees"). Even so,
  only 39% of calls pass.
- **The noisy-line simulation is crude** (randomly dropped or cut words), not real speech
  recognition errors.

## Open issues and what I'd do next

1. **Turn length and naturalness (the biggest gap).** 24 words per turn, naturalness about 3/5.
   Prompt rules haven't moved it across five runs. Next: enforce it in code, either by capping
   the words spoken per turn or by splitting long replies so the agent pauses for the borrower.
2. **The model still says goodbye itself** before ending in about 30% of calls. The safety net
   stops the caller hearing it twice, but the habit remains.
3. **Run 5 misses not yet investigated:** two verification failures (S04, S16; the noisy-line one
   is likely mangled digits) and one `call_dropped` in the waiver scenario.
4. **Voice testing.** See below.

## Voice calls

Tested in the browser (Pipecat SmallWebRTC, Deepgram nova-3, Claude Haiku 5.5, Cartesia), on a laptop with Bluetooth earbuds.

| Metric | Result |
|---|---|
| Calls made | 9 (normal, interruption, silence, hardship ×2, family member, Hindi ×3) |
| Latency: caller stops → agent starts speaking (median / p90 / max) | 1.54s / 1.87s / 4.13s (22 turns, 4 calls, final code) |
| Typical turn breakdown | LLM first token ~0.75s, speech recognition ~0.5s, voice ~0.1–0.25s, plus end-of-turn wait |
| Payment-link tool (2s mock) | Filler line starts within ~1s; no dead air |
| Barge-in | Agent stops as soon as the caller talks over it |
| Silence | Check-in, then repeat, then polite close |
| Family member asking for the amount | Nothing disclosed |
| Hindi switch by voice | Works, natural Hinglish (after fixes below) |

### Failures found only in voice calls

5. **Reply cut off in the hardship flow.** The 300-token reply cap cut off a reply carrying two tool
   calls, so the account might not have been flagged. The evals used the same cap and missed it. Raised to 1000 (a
   ceiling only; speech streams, so no latency cost). In the next call the model said it was flagging
   the account but never called `flag_for_human`. Escalation still happened, because the code forces it for any
   hardship outcome. That is why that rule lives in code, not the prompt.
6. **Said the same thing twice at the close.** The model spoke "a colleague will be in touch" and
   then its closing line said it again. Fix in code: if the model already spoke in that turn, its
   closing line is dropped and only a short goodbye is added.
7. **Hindi sounded like a news reader** ("किश्त… देय थी… बकाया है… योजना"). Rewrote the Hindi
   guidance for everyday Hinglish (EMI, loan, payment kept in English) with example lines, and added an eval check
   for formal Hindi words. In the same calls, switching language made the agent re-ask the name
   (it now continues where it was), speech recognition heard Hindi as Turkish (now locked to
   Hindi after the switch), and the filler and re-ask lines were in English (now in the call's
   language).
8. **4–7 seconds of silence on tool turns.** Per-turn latency showed that almost every slow turn
   (p90 5.1s, max 7.2s) was one where the agent called a tool before speaking (verify, flag
   for hardship, record a promise date). The caller heard nothing until the model had picked
   the tool, the tool had run, and a second LLM call had produced the reply. Fix: a short
   "One moment." / "जी, एक सेकंड।" is spoken as soon as a tool turn starts, unless the
   agent already spoke in that turn. Time to first sound: p90 5.11s → 1.62s. The time to the
   actual answer is unchanged; a real fix would remove the second LLM call or use a faster model.