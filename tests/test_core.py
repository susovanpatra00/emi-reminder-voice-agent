import asyncio
from datetime import date

from agent.borrowers import get_borrower, load_borrowers
from agent.call_session import CallSession
from agent.speech import extract_digits, number_to_words, rupees, spoken_date


def run(coro):
    return asyncio.run(coro)


def test_indian_numbers():
    assert rupees(12500) == "twelve thousand five hundred rupees"
    assert number_to_words(125000) == "one lakh twenty-five thousand"
    assert number_to_words(38750) == "thirty-eight thousand seven hundred fifty"
    assert number_to_words(2199) == "two thousand one hundred ninety-nine"
    assert number_to_words(12_50_00_000) == "twelve crore fifty lakh"


def test_dates():
    today = date(2026, 10, 30)
    assert spoken_date(date(2026, 11, 5), today) == "the fifth of November"
    assert spoken_date(date(2026, 10, 31), today) == "tomorrow"
    assert spoken_date(date(2027, 1, 2), today) == "the second of January two thousand twenty-seven"


def test_extract_digits():
    assert extract_digits("four eight two one") == "4821"
    assert extract_digits("48 21") == "4821"
    assert extract_digits("char aath do ek") == "4821"
    assert extract_digits("double three nine zero") == "3390"
    assert extract_digits("चार आठ दो एक") == "4821"


def test_data_consistent():
    for b in load_borrowers().values():
        overdue = (date(2026, 10, 30) - b.due_date).days
        assert b.days_overdue == max(0, overdue), b.borrower_id


def test_no_loan_tools_before_verification():
    s = CallSession(get_borrower("B001"), link_delay_secs=0)
    assert run(s.handle("send_payment_link", {}))["error"] == "not_verified"
    assert run(s.handle("record_promise_to_pay", {"date_iso": "2026-11-07"}))["error"] == "not_verified"
    assert not s.record.payment_link_sent


def test_verification_two_attempts():
    s = CallSession(get_borrower("B001"))
    r = run(s.handle("verify_identity", {"method": "phone_last4", "value": "1234"}))
    assert r["verified"] is False and "loan" not in r
    r = run(s.handle("verify_identity", {"method": "phone_last4", "value": "0000"}))
    assert r["attempts_left"] == 0
    r = run(s.handle("verify_identity", {"method": "phone_last4", "value": "4821"}))
    assert r["verified"] is False and "loan" not in r  # locked even with the right answer


def test_verify_then_facts():
    s = CallSession(get_borrower("B002"))
    r = run(s.handle("verify_identity", {"method": "dob", "value": "1987-08-22"}))
    assert r["verified"]
    assert r["loan"]["emi_amount"] == "four thousand three hundred fifty rupees"
    assert "ten days" in r["loan"]["status"]


def test_ptp_and_escalation_rules():
    s = CallSession(get_borrower("B001"), link_delay_secs=0)
    run(s.handle("verify_identity", {"method": "phone_last4", "value": "four eight two one"}))
    assert run(s.handle("record_promise_to_pay", {"date_iso": "2026-10-01"}))["error"] == "date_in_past"
    r = run(s.handle("record_promise_to_pay", {"date_iso": "2026-11-06"}))
    assert r["read_back"] == "Friday, the sixth of November"
    run(s.handle("end_call", {"outcome": "hardship", "closing_line": "Take care."}))
    assert s.record.escalate_to_human and s.ended


def test_link_failure_then_retry():
    s = CallSession(get_borrower("B001"), link_failure_mode="first", link_delay_secs=0)
    run(s.handle("verify_identity", {"method": "phone_last4", "value": "4821"}))
    assert run(s.handle("send_payment_link", {}))["sent"] is False
    assert run(s.handle("send_payment_link", {}))["sent"] is True
    assert s.record.payment_link_sent


def test_facts_have_no_digits():
    import re
    s = CallSession(get_borrower("B001"), link_delay_secs=0)
    r = run(s.handle("verify_identity", {"method": "phone_last4", "value": "4821"}))
    assert not any(re.search(r"\d", v) for v in r["loan"].values()), r["loan"]
    link = run(s.handle("send_payment_link", {}))
    assert link["to"] == "the number ending four eight two one"
