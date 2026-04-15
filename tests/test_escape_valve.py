"""Tests for alchemist.implementer.escape_valve."""

from __future__ import annotations

import os

import pytest

from alchemist.implementer.escape_valve import (
    EscapeValveConfig,
    try_escape_valve,
)


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("ALCHEMIST_ESCAPE_ENDPOINT", "http://remote:8080/v1")
    monkeypatch.setenv("ALCHEMIST_ESCAPE_API_KEY", "sk-test")
    monkeypatch.setenv("ALCHEMIST_ESCAPE_MODEL", "gpt-4")
    cfg = EscapeValveConfig.from_env()
    assert cfg.configured
    assert cfg.endpoint == "http://remote:8080/v1"
    assert cfg.api_key == "sk-test"
    assert cfg.model == "gpt-4"


def test_config_not_configured_when_missing(monkeypatch):
    monkeypatch.delenv("ALCHEMIST_ESCAPE_ENDPOINT", raising=False)
    monkeypatch.delenv("ALCHEMIST_ESCAPE_MODEL", raising=False)
    cfg = EscapeValveConfig.from_env()
    assert not cfg.configured


def test_try_escape_valve_returns_none_when_not_configured(tmp_path):
    cfg = EscapeValveConfig()
    result = try_escape_valve(tmp_path, config=cfg)
    assert result is None


def test_try_escape_valve_never_silently_calls_remote(tmp_path, monkeypatch):
    """When env vars are unset, try_escape_valve must return None, not raise."""
    monkeypatch.delenv("ALCHEMIST_ESCAPE_ENDPOINT", raising=False)
    monkeypatch.delenv("ALCHEMIST_ESCAPE_MODEL", raising=False)
    monkeypatch.delenv("ALCHEMIST_ESCAPE_API_KEY", raising=False)
    result = try_escape_valve(tmp_path)
    assert result is None
