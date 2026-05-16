"""Thin LLM wrapper for the ai-daily pipeline.

Two interchangeable backends, selected in config/llm.yaml (or overridden by
the AI_DAILY_LLM_BACKEND environment variable):

  * "claude-cli"     -- shells out to the local Claude Code CLI (`claude -p`).
                        No API key required; this is the default.
  * "anthropic-sdk"  -- uses the Anthropic SDK and ANTHROPIC_API_KEY.

A stage constructs ``LLM(role)`` where role is "extract", "cluster" or
"synthesize"; the wrapper maps the role to the configured model. The prompt is
passed on stdin, so prompt size is not bounded by command-line length limits.

NOTE: like common.py, llm.py is a small deliberate addition to the spec's
layout -- it is the "thin wrapper so models can be swapped" the spec asked for.
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from common import get_logger, load_config


class LLMError(RuntimeError):
    """Raised when an LLM call fails or returns unusable output."""


# alias -> current full model id, for the Anthropic SDK backend
_SDK_ALIASES = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
}


def parse_json(text: str):
    """Parse a JSON value from LLM output, tolerating code fences and prose."""
    if not text or not text.strip():
        raise LLMError("LLM returned empty output")
    t = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.DOTALL)
    if fenced:
        t = fenced.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # Fallback: grab the outermost array or object and try again.
    for open_c, close_c in (("[", "]"), ("{", "}")):
        i, j = t.find(open_c), t.rfind(close_c)
        if 0 <= i < j:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"could not parse JSON from LLM output: {text[:200]}")


class LLM:
    """A role-bound handle to the configured LLM backend."""

    def __init__(self, role: str, *, config=None, logger=None):
        cfg = config or load_config("llm.yaml")
        self._cfg = cfg
        self.role = role
        self.backend = (os.getenv("AI_DAILY_LLM_BACKEND")
                        or cfg.get("backend", "claude-cli"))
        self.model = cfg.get("models", {}).get(role, "haiku")
        self.logger = logger or get_logger()

    def describe(self) -> str:
        return f"{self.backend}:{self.model}"

    def complete(self, prompt: str) -> str:
        """Run a single completion and return the model's text output."""
        if self.backend == "claude-cli":
            return self._claude_cli(prompt)
        if self.backend == "anthropic-sdk":
            return self._anthropic_sdk(prompt)
        raise LLMError(f"unknown LLM backend '{self.backend}' "
                       "(expected 'claude-cli' or 'anthropic-sdk')")

    # --- backends ----------------------------------------------------------
    def _claude_cli(self, prompt: str) -> str:
        cli = self._cfg.get("claude_cli", {})
        cmd = cli.get("command", "claude")
        timeout = cli.get("timeout_seconds", 240)
        budget = cli.get("max_budget_usd")
        # --tools "" disables all agent tools: these calls are pure text/JSON
        # generation, so the model must not wander into tool use (which is
        # slow, costly, and can trip the budget cap mid-generation).
        args = [cmd, "-p", "--model", str(self.model),
                "--output-format", "json", "--no-session-persistence",
                "--tools", ""]
        if budget:
            args += ["--max-budget-usd", str(budget)]
        try:
            proc = subprocess.run(args, input=prompt, capture_output=True,
                                  text=True, encoding="utf-8", timeout=timeout)
        except FileNotFoundError as e:
            raise LLMError(
                f"Claude CLI '{cmd}' not found on PATH. Install Claude Code, "
                "or set claude_cli.command in config/llm.yaml.") from e
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"Claude CLI timed out after {timeout}s") from e
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise LLMError(f"Claude CLI exited {proc.returncode}: {err[:400]}")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"Claude CLI returned non-JSON output: {proc.stdout[:300]}") from e
        if data.get("is_error"):
            raise LLMError(f"Claude CLI reported an error: {data.get('result')}")
        cost = data.get("total_cost_usd")
        if cost is not None:
            self.logger.debug(f"llm[{self.describe()}] call cost ~${cost:.4f}")
        return data.get("result", "")

    def _anthropic_sdk(self, prompt: str) -> str:
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("anthropic package not installed "
                           "(pip install anthropic)") from e
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        sdk_cfg = self._cfg.get("anthropic_sdk", {})
        model = _SDK_ALIASES.get(str(self.model), str(self.model))
        client = anthropic.Anthropic(api_key=key)
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=sdk_cfg.get("max_tokens", 4096),
                messages=[{"role": "user", "content": prompt}])
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Anthropic SDK call failed: {e}") from e
        return "".join(getattr(b, "text", "") for b in msg.content)
