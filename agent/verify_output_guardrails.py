"""Regression checks for the streamed ETF research output guardrail."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import yaml

from nat_streaming_react.text_guardrails import TextGuardrailsMiddleware

CONFIG_PATH = Path(__file__).with_name("config.yml")

# Exactly the shape of a real answer. Every one of these fields must survive the
# output rail: an ISIN, an expense ratio and a fund size are the evidence a
# research decision rests on, and a rail that eats them makes the system useless
# while looking like it is working.
ETF_RESEARCH_OUTPUT = """\
1. **VWCE-XETRA** — Vanguard FTSE All-World UCITS ETF (USD) Accumulating
   - **ISIN:** IE00BK5BQT80
   - **Investment score:** 87/100 (quality and profile fit, not a return forecast)
   - **Decision:** shortlist
   - **TER:** 0.22%
   - **Fund size:** $28,000,000,000
   - **Holdings:** 3,600, top ten 20.0%
   - **Data as of:** 2026-06-30
"""


def main() -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    middleware_cfg = config["middleware"]["workflow_guardrails"]
    guardrails = middleware_cfg["guardrails"]
    output = guardrails["rails"]["output"]

    assert middleware_cfg["stream_output_rails"] is True
    assert output["flows"] == ["regex check output"]
    assert output["streaming"]["enabled"] is True
    assert output["streaming"]["stream_first"] is False
    assert output["streaming"]["chunk_size"] <= 16
    assert "sensitive_data_detection" not in guardrails["rails"].get("config", {})

    patterns = guardrails["rails"]["config"]["regex_detection"]["output"]["patterns"]
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    assert not [p.pattern for p in compiled if p.search(ETF_RESEARCH_OUTPUT)], (
        "Structured ETF research evidence must not trigger the secret rail"
    )

    secret_cases = [
        "api_key=supersecretvalue12345",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
        "AKIAABCDEFGHIJKLMNOP",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "sk-abcdefghijklmnopqrstuvwxyz123456",
        "-----BEGIN PRIVATE KEY-----",
    ]
    for secret in secret_cases:
        assert any(pattern.search(secret) for pattern in compiled), secret

    source = inspect.getsource(TextGuardrailsMiddleware._stream_with_output_rails)
    assert "stream_async" in source, (
        "NeMo streaming output rails must remain enabled"
    )
    assert "buffered_items" not in source, (
        "Output middleware must not buffer the whole assistant response"
    )
    # The rail must run on a leased instance, never the shared one: two concurrent
    # streams sharing an LLMRails let a credential in one be checked against the
    # other's text and released. Proven in verify_guardrails_rails.py.
    assert "self.rails_pool.acquire()" in source, (
        "the streaming output rail must lease an isolated rails instance"
    )
    assert "self._llm_rails.stream_async" not in source, (
        "the streaming output rail is back on the process-wide shared instance"
    )

    print("PASS: structured ETF evidence does not trigger output secret patterns")
    print("PASS: credential/private-key leakage patterns remain blocked")
    print("PASS: NeMo output rails are incremental/streamed again")
    print("PASS: the streaming rail runs on a per-request rails instance")


if __name__ == "__main__":
    main()
