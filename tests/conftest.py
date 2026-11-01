import os

# Unit tests check exact dates against the mock data, so pin the call date.
os.environ.setdefault("CALL_DATE", "2026-10-30")
