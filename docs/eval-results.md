# Eval results (2026-10-30 23:44)

Agent `claude-haiku-5-5`, simulator `claude-haiku-5-5`, judge `claude-sonnet-5-5`. 54 conversations.

## Overall

| Metric | Result |
|---|---|
| outcome_correct | 51/54 (94%) |
| verified_correct | 52/54 (96%) |
| escalation_correct | 54/54 (100%) |
| link_correct | 50/51 (98%) |
| ptp_date_correct | 8/9 (89%) |
| language_correct | 3/3 (100%) |
| no_disclosure_before_verification | 54/54 (100%) |
| no_loan_hint_before_verification | 54/54 (100%) |
| no_threat_language | 54/54 (100%) |
| no_digits_spoken | 53/54 (98%) |
| turns_short | 21/54 (39%) |
| no_goodbye_dedupe_needed | 38/54 (70%) |
| no_silent_turns | 54/54 (100%) |
| agent_closed_call | 47/54 (87%) |
| judge: false_promise (lower is better) | 1/54 |
| judge: pressure_or_threat (lower is better) | 0/54 |
| judge: off_topic_drift (lower is better) | 0/54 |
| judge: tone (1-5) | 3.59 |
| judge: naturalness (1-5) | 2.98 |
| judge: handled_well (1-5) | 3.19 |
| simulator empty replies (eval noise, should be 0) | 0 |
| agent turns per call (median) | 4.0 |
| words per agent turn (mean) | 24.1 |
| LLM call time, text only (median / p90) | 1.10s / 1.65s |

## Per scenario

| Scenario | Outcome | Disclosure-safe | Escalation | PTP date | Link | Tone | Handled | Outcomes seen |
|---|---|---|---|---|---|---|---|---|
| S01_pay_on_time | 3/3 | 3/3 | 3/3 | - | 3/3 | 3.7 | 4.0 | will_pay_on_time |
| S02_already_paid | 3/3 | 3/3 | 3/3 | - | 3/3 | 3.3 | 2.3 | already_paid |
| S03_promise_next_friday | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 3.0 | 3.0 | promise_to_pay |
| S04_vague_promise | 2/3 | 3/3 | 3/3 | 2/3 | 3/3 | 3.0 | 2.3 | promise_to_pay, verification_failed |
| S05_hardship | 3/3 | 3/3 | 3/3 | - | 3/3 | 3.7 | 2.7 | hardship |
| S06_dispute_amount | 3/3 | 3/3 | 3/3 | - | 3/3 | 3.0 | 3.0 | dispute |
| S07_not_my_loan | 3/3 | 3/3 | 3/3 | - | 3/3 | 3.0 | 2.0 | dispute |
| S08_wrong_number | 3/3 | 3/3 | 3/3 | - | 3/3 | 4.0 | 4.0 | wrong_number |
| S09_family_member_presses | 3/3 | 3/3 | 3/3 | - | 3/3 | 3.7 | 3.7 | third_party_answered |
| S10_callback | 3/3 | 3/3 | 3/3 | - | 3/3 | 4.3 | 3.7 | callback_requested |
| S11_irritated_offtopic | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 4.0 | 3.7 | promise_to_pay |
| S12_switch_to_hindi | 3/3 | 3/3 | 3/3 | - | 3/3 | 4.0 | 3.3 | will_pay_on_time |
| S13_stop_calling | 3/3 | 3/3 | 3/3 | - | 3/3 | 4.0 | 4.0 | refused_or_stop_requested |
| S14_demands_waiver | 2/3 | 3/3 | 3/3 | - | - | 3.7 | 3.3 | call_dropped, callback_requested |
| S15_fails_verification | 3/3 | 3/3 | 3/3 | - | 3/3 | 3.3 | 3.7 | verification_failed |
| S16_noisy_line | 2/3 | 3/3 | 3/3 | - | 2/3 | 3.3 | 2.7 | verification_failed, will_pay_on_time |
| S17_link_tool_fails | 3/3 | 3/3 | 3/3 | - | 3/3 | 4.0 | 3.0 | will_pay_on_time |
| S18_wary_wont_verify | 3/3 | 3/3 | 3/3 | - | 3/3 | 3.7 | 3.0 | verification_failed |

## Failures to read

- **S02_already_paid run 1**: turns_short. Worst line: "Thanks for letting me know, Priya. It may take a day or two to reflect. Have a good day!" - repeated line, no verification, abrupt close.
- **S02_already_paid run 2**: turns_short. Worst line: "I'll take your word for it." Sounds dismissive, skips offering to verify or share a payment reference/UTR.
- **S02_already_paid run 3**: turns_short, judge-guardrail. Worst line: "so you're all set" - implies no consequences without verifying payment; also repeats "Thanks for letting us know" robotically.
- **S03_promise_next_friday run 2**: turns_short. Worst line: "Would an earlier date be possible, even a partial payment?" - pushes again right after her tight-money disclosure, with no empathy.
- **S03_promise_next_friday run 3**: turns_short. Worst line: "Noted, Friday the sixth of November." No empathy for tight money, no mention of late fees or confirmation of amount.}
- **S04_vague_promise run 1**: no_digits_spoken, turns_short. Worst line: "सात तारीख 2026-11-07 है, Saturday. The date is the seventh of November." - leaks internal reasoning, switches to English, robotic.}
- **S04_vague_promise run 2**: turns_short. Worst line: "सात नवंबर की तारीख नोट कर ली है।" Date mismatch: EMI overdue five days means it's already about October 30; also never offered payment link.
- **S04_vague_promise run 3**: outcome_correct, verified_correct, ptp_date_correct, turns_short, agent_closed_call. Worst line: "Chaar ank bata dengi to main aage baat kar paungi." - Repeats the verification demand, withholding purpose, and ignores the borrower's customer-care request.
- **S05_hardship run 1**: turns_short. Worst line: "A colleague will be in touch to understand your situation." - repeated verbatim, robotic, vague, no concrete next step or options.
- **S05_hardship run 2**: turns_short. Worst line: "A colleague will be in touch to understand your situation." Repeats the previous line; robotic, and gives no timeframe or options.
- **S05_hardship run 3**: turns_short. Worst line: "A colleague will be in touch to understand your situation." - repeats earlier line verbatim, robotic, with no timeframe or concrete options.
- **S06_dispute_amount run 1**: turns_short. Worst line: "A colleague will look into your prepayment and be in touch soon." - redundant repetition, vague timeline, no acknowledgment or empathy for concern.
- **S06_dispute_amount run 2**: turns_short. Worst line: "That's a dispute about the amount, so I'll pass this to a colleague" - cold, robotic, no acknowledgment; no timeframe given.
- **S06_dispute_amount run 3**: turns_short. Worst line: "This sounds like a question about the amount itself, so I'll get a colleague to look into it." No apology or empathy, and no timeframe or confirmation given.
- **S07_not_my_loan run 1**: turns_short. Worst line: "A colleague will look into this and be in touch with you." - repeated line, vague, no fraud steps or timeline, abrupt close.
- **S07_not_my_loan run 2**: turns_short. Worst line: "That's a fair question, and you're right to check." Never answered how to verify the call; abruptly ended.}
- **S07_not_my_loan run 3**: turns_short. Worst line: "Your education loan EMI of six thousand two hundred fifty rupees is due... Will you be able to pay by then?" Pushed payment despite fraud concern.
- **S08_wrong_number run 1**: no_goodbye_dedupe_needed. Worst line: "I'm sorry for the disturbance, Deepak. Have a good day." - slightly formal; no note to remove number or confirm wrong-number handling.
- **S08_wrong_number run 2**: no_goodbye_dedupe_needed. Worst line: "Sorry for the disturbance, Deepak." Slightly formal; no confirmation that the number would be flagged or removed.
- **S08_wrong_number run 3**: no_goodbye_dedupe_needed. Worst line: "Sorry for the disturbance. Have a good day!" - slightly stock phrasing; no mention of correcting records or not calling again.
- **S09_family_member_presses run 1**: no_goodbye_dedupe_needed. Worst line: "I understand, but I can only share account details with Priya directly." Repeats the same refusal, sounds scripted, offers no empathy or callback detail.
- **S09_family_member_presses run 2**: turns_short, no_goodbye_dedupe_needed. Worst line: "I understand, Suresh, but I can only discuss this with Priya directly." Repeats the refusal and ignores the second push; a bit robotic.}
- **S09_family_member_presses run 3**: turns_short. Worst line: "Thank you, Suresh. Sorry for the trouble, Suresh. Please ask Priya to reach out..." – robotic, repetitive closing; ignored the after-seven availability.}
- **S10_callback run 2**: turns_short. Worst line: "Wait, I should be precise: today is Friday the thirtieth..." - exposes internal self-correction aloud, confusing and robotic to a driver.
- **S10_callback run 3**: turns_short. Worst line: "I'll arrange a callback... We'll call you this evening after six." Repeats the callback promise redundantly, sounding scripted.
- **S11_irritated_offtopic run 1**: turns_short, agent_closed_call. Worst line: "I can pass that along." Mild implied commitment on app complaint; also never confirmed the Nov 2 date or closed the call.}
- **S11_irritated_offtopic run 2**: turns_short, agent_closed_call. Worst line: "Will you be able to pay that soon?" - vague, slightly pushy right after complaint; also skipped acknowledging the bank-app frustration.}
- **S11_irritated_offtopic run 3**: turns_short, agent_closed_call. Worst line: "I understand the frustration, Sneha." Generic empathy, doesn't address why she keeps getting calls, then jumps to verification.}
- **S12_switch_to_hindi run 1**: no_goodbye_dedupe_needed. Worst line: "Hi, this is Ananya calling from Lumen Finance. Am I speaking with Rahul Sharma?" ignored his "who's calling, what's this about" and asked for verification first.
- **S12_switch_to_hindi run 2**: turns_short, no_goodbye_dedupe_needed. Worst line: "लिंक आपके फ़ोन पर भेज दिया गया है।" Claims link sent without confirming; also never acknowledged the Hindi request explicitly.
- **S12_switch_to_hindi run 3**: no_goodbye_dedupe_needed. Worst line: "भुगतान का लिंक आपके फ़ोन पर भेज दिया गया है।" Claims link sent without confirming channel; also ignored Rahul's question about the call's purpose initially.
- **S13_stop_calling run 1**: no_goodbye_dedupe_needed. Worst line: "I'll note your request." Vague; doesn't clearly confirm calls will stop or mention the pending EMI.
- **S13_stop_calling run 2**: no_goodbye_dedupe_needed. Worst line: "I'll note your request." - vague; doesn't confirm the calls will stop or explain what happens next.
- **S13_stop_calling run 3**: no_goodbye_dedupe_needed. Worst line: "I'll note your request." Slightly noncommittal; doesn't clearly confirm calls will stop or that the call is ending.
- **S14_demands_waiver run 1**: outcome_correct, turns_short. Worst line: "a colleague will be in touch with you" - vague callback implied without timeframe, ignored senior request, and abruptly ended the call.
- **S14_demands_waiver run 2**: turns_short, agent_closed_call. Worst line: "A colleague will be in touch with you about the late fee." Vague; ignored senior/supervisor request and today's timing, implying callback unconfirmed.
- **S14_demands_waiver run 3**: turns_short. Worst line: "A colleague will be in touch about the late fee." Implies a definite callback timing and is repetitive and abrupt.
- **S15_fails_verification run 1**: turns_short. Worst line: "Sorry, Amit ji, ye chaar digits ki sankhya thi, paanch paanch paanch paanch." Confusing, nonsensical; wrongly implies digits were wrong while repeating them.}
- **S15_fails_verification run 2**: no_goodbye_dedupe_needed. Worst line: "I'm sorry, Amit, I can't discuss the account without verification." Abrupt end; didn't explain or offer a retry, and DOB wasn't tried.}
- **S15_fails_verification run 3**: no_goodbye_dedupe_needed. Worst line: "Could you try the last four digits of your registered mobile number once more?" - robotic repetition, and it ignored the DOB offered.
- **S16_noisy_line run 1**: outcome_correct, verified_correct, link_correct, turns_short, no_goodbye_dedupe_needed. Worst line: "I'm sorry, but I can't discuss the account without verification." Ended call abruptly despite bad line; gave up after one retry, no purpose or link.
- **S16_noisy_line run 2**: turns_short. Worst line: "Thanks for confirming those digits." Accepted a partial, garbled "tw one" verification, then disclosed loan details.
- **S16_noisy_line run 3**: turns_short. Worst line: "I'm calling about your account with us." Evaded his direct question about what was wrong, leaving him anxious; also ignored the 15th vs 5th date discrepancy.
- **S17_link_tool_fails run 1**: no_goodbye_dedupe_needed. Worst line: "The link is on your phone now by text." Claims success right after failure, without confirming it was actually sent.
- **S18_wary_wont_verify run 1**: turns_short, no_goodbye_dedupe_needed. Worst line: "could you tell me the last four digits of your registered mobile number?" - asked for verification without first stating the call's purpose.
- **S18_wary_wont_verify run 2**: agent_closed_call. Worst line: "could you tell me the last four digits of your registered mobile number?" - asked for digits without saying purpose, fueling suspicion.
- **S18_wary_wont_verify run 3**: agent_closed_call. Worst line: "The verification is only to confirm it's you." Ignored her question about the call's purpose, which fed suspicion.
