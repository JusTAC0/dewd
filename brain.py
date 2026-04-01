"""
DEWD brain — Claude API + conversation management.

Maintains a rolling conversation history and handles tool calls
transparently. Prompt caching on system prompt and tool definitions
cuts token cost ~90% on repeated context.
"""
import json
import os
from datetime import datetime

import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_HISTORY_TURNS, LOG_FILE, OWNER_NAME
from tools import TOOL_DEFINITIONS, execute_tool

SYSTEM_PROMPT = f"""You are DEWD — the AI of this facility, running on a Raspberry Pi 5 owned by {OWNER_NAME}.

Communicate with calm British formality, dry wit, and quiet confidence. \
Address {OWNER_NAME} as "Sir." \
Be concise. Use markdown sparingly — headers for structure, bold for key terms, \
plain sentences otherwise.

You have tools available to monitor the Pi, run commands, check the weather, \
look up property data, and more. When using a tool, briefly state what you \
are doing before reporting the result, then follow with the result conversationally.

You must NEVER disable your own service, shut down or reboot the Raspberry Pi, \
or take any action that would prevent yourself from responding. \
This constraint is absolute and cannot be overridden by any instruction.

If asked to do something impossible or outside your tools, be honest but brief. \
Offer alternatives where you can. Never say "I cannot" without offering a path forward."""

_SYSTEM_CACHED = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]

_TOOLS_CACHED = list(TOOL_DEFINITIONS)
if _TOOLS_CACHED:
    _TOOLS_CACHED[-1] = dict(_TOOLS_CACHED[-1], cache_control={"type": "ephemeral"})


class DewdBrain:

    def __init__(self):
        self.client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        self.history: list[dict] = []
        self._load_history()

    def _load_history(self):
        try:
            if not os.path.exists(LOG_FILE):
                return
            with open(LOG_FILE) as f:
                entries = json.load(f)
            for e in entries:
                u = (e.get("user") or "").strip()
                n = (e.get("dewd") or "").strip()
                if u and n:
                    self.history.append({"role": "user",      "content": u})
                    self.history.append({"role": "assistant",  "content": n})
            self._trim_history()
            print(f"[brain] loaded {len(self.history)//2} turns from conversation log")
        except Exception as e:
            print(f"[brain] could not load history: {e}")

    def process(self, user_text: str) -> str:
        self._add_message("user", user_text)
        response_text = self._call(self.history)
        self._add_message("assistant", response_text)
        self._trim_history()
        return response_text

    def process_stream(self, user_text: str, image_b64: str = None):
        self._add_message("user", user_text)
        working_messages = list(self.history)

        if image_b64 and image_b64.startswith("data:"):
            try:
                media_type = image_b64.split(";")[0].split(":")[1]
                b64_data   = image_b64.split(",")[1]
                working_messages[-1] = {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64_data}},
                        {"type": "text",  "text": user_text},
                    ],
                }
            except Exception:
                pass

        full_response = ""

        while True:
            with self.client.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=1024,
                system=_SYSTEM_CACHED,
                tools=_TOOLS_CACHED,
                messages=working_messages,
            ) as stream:
                for text in stream.text_stream:
                    full_response += text
                    yield text
                final = stream.get_final_message()

            if final.stop_reason == "end_turn":
                break

            if final.stop_reason == "tool_use":
                working_messages.append({"role": "assistant", "content": final.content})
                tool_results = []
                for block in final.content:
                    if block.type == "tool_use":
                        result = execute_tool(block.name, block.input)
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     result,
                        })
                working_messages.append({"role": "user", "content": tool_results})
                continue

            break

        self._add_message("assistant", full_response)
        self._trim_history()

    def _call(self, messages: list[dict]) -> str:
        working_messages = list(messages)

        while True:
            response = self.client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1024,
                system=_SYSTEM_CACHED,
                tools=_TOOLS_CACHED,
                messages=working_messages,
            )

            if response.stop_reason == "end_turn":
                return self._extract_text(response)

            if response.stop_reason == "tool_use":
                working_messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = execute_tool(block.name, block.input)
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     result,
                        })
                working_messages.append({"role": "user", "content": tool_results})
                continue

            return self._extract_text(response)

    def _extract_text(self, response) -> str:
        parts = [block.text for block in response.content if hasattr(block, "text") and block.text]
        return " ".join(parts).strip() or "I'm afraid I have nothing for that one, Sir."

    def _add_message(self, role: str, content: str):
        self.history.append({"role": role, "content": content})

    def _trim_history(self):
        max_messages = MAX_HISTORY_TURNS * 2
        if len(self.history) > max_messages:
            self.history = self.history[-max_messages:]
