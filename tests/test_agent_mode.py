"""Agent mode (AGENT_CLIPS=1): cutting needs no LLM key, and the dashboard is told so."""
import asyncio

import pytest
from fastapi.testclient import TestClient

import app as app_module
import llm_backend


@pytest.fixture
def no_llm(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(llm_backend, "active", lambda: False)


def _process_without_key():
    return TestClient(app_module.app).post("/api/process", data={})


def test_config_reports_agent_mode(monkeypatch):
    monkeypatch.setattr(app_module, "AGENT_CLIPS", True)
    assert asyncio.run(app_module.get_config())["agentClips"] is True
    monkeypatch.setattr(app_module, "AGENT_CLIPS", False)
    assert asyncio.run(app_module.get_config())["agentClips"] is False


def test_process_without_key_is_refused_outside_agent_mode(monkeypatch, no_llm):
    monkeypatch.setattr(app_module, "AGENT_CLIPS", False)
    r = _process_without_key()
    assert r.status_code == 400 and "Gemini" in r.text


def test_process_without_key_passes_the_key_gate_in_agent_mode(monkeypatch, no_llm):
    monkeypatch.setattr(app_module, "AGENT_CLIPS", True)
    r = _process_without_key()
    # The request is still incomplete (no URL or file), but it is no longer rejected for the key.
    assert "Gemini" not in r.text
