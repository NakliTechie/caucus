"""Sovereign posture (§7.5): no phone-home, no telemetry — pinned before LiteLLM imports."""

import os

import litellm

import caucus  # noqa: F401 - import sets the env before litellm loads


def test_cost_map_is_local_no_network_fetch():
    assert os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") == "True"


def test_litellm_telemetry_disabled():
    assert litellm.telemetry is False
