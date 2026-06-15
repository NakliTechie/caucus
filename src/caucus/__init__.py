"""Caucus — a local, sovereign turn-aware synth proxy between a coding agent and the models.

Sovereignty starts at import time: before anything pulls in LiteLLM we pin it to its
*bundled* model-cost map and disable its feedback box, so the daemon never reaches the
network except to a provider the user chose (zero telemetry / no phone-home).
"""

from __future__ import annotations

import os as _os

# No phone-home — use LiteLLM's packaged cost map instead of fetching it from GitHub.
_os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
_os.environ.setdefault("LITELLM_DONT_SHOW_FEEDBACK_BOX", "True")

__version__ = "0.1.0.dev0"
