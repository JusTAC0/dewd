"""
DEWD Smith Agent

DEWD's autonomous senior engineer. Audits the codebase, finds bugs, patches
them through a full agentic tool loop, verifies each fix via py_compile and
cascade import checks, then ships to live.

Phase 1 — Haiku director: scans changed source files, produces ranked findings.
Phase 2 — Sonnet engineer: agentic tool loop, one finding at a time, max 3 write
           attempts per finding. Cascade check after every patch.

Triggered by Frontier after each Frontier run. Can run standalone.
Morning runs (SMITH_BRIEF_WINDOW) build and push the daily brief via ntfy.
Permanent change log appended to ~/Desktop/smith_log.md
Writes run state to data/agents/smith.json
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import anthropic

from config import (
    ANTHROPIC_API_KEY, DATA_DIR, AGENTS_DIR, BLUEPRINTS_DIR,
    SMITH_LOG_PATH, SMITH_BRIEF_WINDOW,
    OWNER_NAME,
)
from notify import send_alert
from agents.common import get_logger as _get_logger
from agents.common import atomic_write, write_status, write_error, ET_TZ as _ET

log = _get_logger(__name__)

HAIKU_MODEL  = "claude-haiku-4-5-20251001"
SONNET_MODEL = "claude-sonnet-4-6"
OUTPUT_FILE  = os.path.join(AGENTS_DIR, "smith.json")
SEEN_FILE    = os.path.join(AGENTS_DIR, "seen.json")
PROJECT_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_VENV_PY = os.path.join(PROJECT_DIR, "venv", "bin", "python3")
PYTHON_BIN = _VENV_PY if os.path.exists(_VENV_PY) else "python3"

EXCLUDE_DIRS  = {"venv", "__pycache__", ".git", ".mypy_cache", "node_modules", "data"}
EXCLUDE_FILES = {"smith.py"}

MAX_ATTEMPTS_PER_FINDING = 3
MAX_TOOL_ITERATIONS      = 25


def _load_seen() -> dict:
    try:
        with open(SEEN_FILE) as f:
            return json.load(f)
    except Exception:
        return {
            "frontier": {"repos": {}, "packages": {}, "articles": {}},
            "smith":    {"bugs": {}, "file_fingerprints": {}},
        }


def _save_seen(seen: dict):
    atomic_write(SEEN_FILE, seen)


def _file_fingerprint(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()


def _mark_bug_seen(seen: dict, finding: dict, status: str):
    smith_seen = seen.setdefault("smith", {"bugs": {}, "file_fingerprints": {}})
    smith_seen.setdefault("bugs", {})[finding["id"]] = {
        "title":    finding["title"],
        "file":     finding["file"],
        "status":   status,
        "seen_at":  datetime.now(timezone.utc).isoformat(),
    }
    _save_seen(seen)


def _update_fingerprints(source_files: dict, seen: dict):
    smith_seen = seen.setdefault("smith", {"bugs": {}, "file_fingerprints": {}})
    fps = smith_seen.setdefault("file_fingerprints", {})
    for path, content in source_files.items():
        fps[path] = _file_fingerprint(content)
    _save_seen(seen)


def _scan_project_files() -> dict:
    """Return {relative_path: content} for all scannable .py files."""
    files = {}
    for root, dirs, filenames in os.walk(PROJECT_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.endswith(".py") or fn in EXCLUDE_FILES:
                continue
            fpath = os.path.join(root, fn)
            rel   = os.path.relpath(fpath, PROJECT_DIR)
            try:
                with open(fpath) as f:
                    files[rel] = f.read()
            except Exception:
                pass
    return files


def _resolve_path(path: str) -> str:
    """Resolve relative-to-project-root or absolute path."""
    return path if os.path.isabs(path) else os.path.join(PROJECT_DIR, path)


_PHASE1_SYSTEM = [{
    "type": "text",
    "text": (
        "You are Smith Phase 1 — DEWD's code auditor. "
        "Scan the provided source files for real, actionable bugs and defects. "
        "Do not report style issues, refactor suggestions, or 'could be improved' items. "
        "A finding must have a clear, demonstrable wrong behavior.\n\n"
        "Ranking formula: (severity_base × execution_multiplier) + blast_radius + silent_failure_modifier\n"
        "  severity_base:            Critical=5  High=4  Medium=3  Low=2  Info=1\n"
        "  execution_multiplier:     Always=2.0  Common=1.5  Edge=1.0  Rare=0.5\n"
        "  blast_radius:             System-wide=3  Multi-file=2  Single=1  Isolated=0\n"
        "  silent_failure_modifier:  Data loss/wrong output=+3  Logs+continues=+1  Raises=+0\n\n"
        "Output ONLY valid JSON. No prose, no markdown fences."
    ),
    "cache_control": {"type": "ephemeral"},
}]


def phase1_audit(source_files: dict, seen: dict) -> list:
    """
    Haiku scans changed source files and returns a ranked list of findings.
    Only changed files (compared to stored fingerprints) are sent for analysis.
    """
    smith_seen = seen.get("smith", {})
    file_fps   = smith_seen.get("file_fingerprints", {})
    bugs_seen  = smith_seen.get("bugs", {})

    changed_files  = {}
    unchanged_list = []
    for path, content in source_files.items():
        if file_fps.get(path) != _file_fingerprint(content):
            changed_files[path] = content
        else:
            unchanged_list.append(path)

    if not changed_files:
        log.info("  [smith/phase1] all files unchanged — skipping audit")
        return []

    file_sections = [
        f"### {path}\n```python\n{content}\n```"
        for path, content in changed_files.items()
    ]

    prompt = f"""Audit these DEWD Python source files for real bugs.

UNCHANGED FILES (already audited, skip): {json.dumps(unchanged_list)}
ALREADY-SEEN BUGS (skip unless reintroduced): {json.dumps(list(bugs_seen.keys())[:20])}

## FILES TO AUDIT:

{"\\n\\n".join(file_sections)}

---

Return ONLY this JSON:
{{
  "findings": [
    {{
      "id": "short-kebab-case-id",
      "file": "relative/path.py",
      "function": "function_name or module-level",
      "line_hint": 42,
      "title": "one-line bug title",
      "description": "what is wrong and exactly why it fails",
      "severity": "critical|high|medium|low",
      "execution": "always|common|edge|rare",
      "blast_radius": "system|multi|single|isolated",
      "silent_failure": "yes|partial|no",
      "rank_score": 12.5,
      "fix_hint": "what the correct fix looks like"
    }}
  ]
}}

Sort by rank_score descending. Only real bugs."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=2500,
        system=_PHASE1_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(raw).get("findings", [])
    except Exception as e:
        log.error(f"  [smith/phase1] JSON parse error: {e} — raw: {raw[:200]}")
        return []


SMITH_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a DEWD project file. Path relative to project root or absolute.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (e.g. 'agents/daymark.py')"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write file content. Creates a .bak backup first. "
            "Always write the complete file — never partial content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string",  "description": "File path relative to project root or absolute"},
                "content": {"type": "string",  "description": "Complete new file content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_py_compile",
        "description": "Syntax-check a Python file with py_compile. Returns 'OK' or the error.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to project root or absolute"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "run_import_check",
        "description": "Test that a Python module imports without error. Use dot notation (e.g. 'agents.daymark').",
        "input_schema": {
            "type": "object",
            "properties": {
                "module": {"type": "string", "description": "Module name in dot notation"}
            },
            "required": ["module"],
        },
    },
    {
        "name": "grep_codebase",
        "description": "Search for a string or pattern across all DEWD .py files. Returns matches with file:line context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search string or regex pattern"}
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_project_files",
        "description": "List all .py source files in the DEWD project (excluding venv, cache, data).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def _execute_tool(tool_name: str, tool_input: dict) -> str:
    if tool_name == "read_file":
        path = _resolve_path(tool_input["path"])
        try:
            with open(path) as f:
                return f.read()
        except Exception as e:
            return f"ERROR: {e}"

    elif tool_name == "write_file":
        path    = _resolve_path(tool_input["path"])
        content = tool_input["content"]
        if not path.startswith(PROJECT_DIR):
            return "ERROR: Write refused — path outside project directory"
        if os.path.basename(path) == "smith.py":
            return "ERROR: Write refused — Smith cannot modify itself"
        try:
            if os.path.exists(path):
                with open(path) as f:
                    orig = f.read()
                with open(path + ".bak", "w") as f:
                    f.write(orig)
            with open(path, "w") as f:
                f.write(content)
            return f"OK: wrote {len(content)} bytes"
        except Exception as e:
            return f"ERROR: {e}"

    elif tool_name == "run_py_compile":
        path = _resolve_path(tool_input["path"])
        try:
            result = subprocess.run(
                [PYTHON_BIN, "-m", "py_compile", path],
                capture_output=True, text=True, timeout=15, cwd=PROJECT_DIR,
            )
            return "OK" if result.returncode == 0 else (result.stderr.strip() or "Compile error")
        except subprocess.TimeoutExpired:
            return "ERROR: timed out"
        except Exception as e:
            return f"ERROR: {e}"

    elif tool_name == "run_import_check":
        module = tool_input["module"]
        try:
            result = subprocess.run(
                [PYTHON_BIN, "-c", f"import {module}"],
                capture_output=True, text=True, timeout=15,
                cwd=PROJECT_DIR,
                env={**os.environ, "PYTHONPATH": PROJECT_DIR},
            )
            return "OK" if result.returncode == 0 else (result.stderr.strip() or "Import failed")
        except subprocess.TimeoutExpired:
            return "ERROR: timed out"
        except Exception as e:
            return f"ERROR: {e}"

    elif tool_name == "grep_codebase":
        pattern = tool_input["pattern"].lower()
        matches = []
        for root, dirs, filenames in os.walk(PROJECT_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                fpath = os.path.join(root, fn)
                rel   = os.path.relpath(fpath, PROJECT_DIR)
                try:
                    with open(fpath) as f:
                        for i, line in enumerate(f, 1):
                            if pattern in line.lower():
                                matches.append(f"{rel}:{i}: {line.rstrip()}")
                except Exception:
                    pass
        return "\n".join(matches[:50]) if matches else "No matches found"

    elif tool_name == "list_project_files":
        files = []
        for root, dirs, filenames in os.walk(PROJECT_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for fn in filenames:
                if fn.endswith(".py"):
                    files.append(os.path.relpath(os.path.join(root, fn), PROJECT_DIR))
        return "\n".join(sorted(files))

    return f"ERROR: Unknown tool '{tool_name}'"


def _run_cascade_check(patched_path: str) -> tuple:
    """
    py_compile the patched file, then compile every file that imports from it.
    Returns (all_ok: bool, error_message: str).
    """
    abs_path = _resolve_path(patched_path)
    result = subprocess.run(
        [PYTHON_BIN, "-m", "py_compile", abs_path],
        capture_output=True, text=True, timeout=15, cwd=PROJECT_DIR,
    )
    if result.returncode != 0:
        return False, f"{patched_path}: {result.stderr.strip()}"

    module_short = os.path.basename(os.path.splitext(patched_path)[0])
    import_patterns = (
        f"import {module_short}",
        f"from {module_short} import",
        f"from agents.{module_short} import",
        f"import agents.{module_short}",
    )
    for root, dirs, filenames in os.walk(PROJECT_DIR):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fpath = os.path.join(root, fn)
            if fpath == abs_path:
                continue
            try:
                with open(fpath) as f:
                    content = f.read()
                if any(pat in content for pat in import_patterns):
                    dep = subprocess.run(
                        [PYTHON_BIN, "-m", "py_compile", fpath],
                        capture_output=True, text=True, timeout=15, cwd=PROJECT_DIR,
                    )
                    if dep.returncode != 0:
                        rel = os.path.relpath(fpath, PROJECT_DIR)
                        return False, f"Cascade failure in {rel}: {dep.stderr.strip()}"
            except Exception:
                pass
    return True, ""


def _restore_backup(path: str):
    abs_path = _resolve_path(path)
    bak = abs_path + ".bak"
    if os.path.exists(bak):
        try:
            with open(bak) as f:
                content = f.read()
            with open(abs_path, "w") as f:
                f.write(content)
            log.info(f"  [smith] restored {path} from .bak")
        except Exception as e:
            log.error(f"  [smith] backup restore failed: {e}")


_PHASE2_SYSTEM = [{
    "type": "text",
    "text": (
        "You are Smith — DEWD's autonomous senior engineer. "
        "You have been given a specific bug to fix. Use your tools to fix it precisely.\n\n"
        "Process:\n"
        "1. read_file — understand the full context around the bug\n"
        "2. write_file — write the corrected file (complete content only, never partial)\n"
        "3. run_py_compile — verify the patched file compiles\n"
        "4. run_py_compile on any dependent files if relevant\n"
        "5. Output your final message starting with exactly 'FIXED:' or 'FAILED:'\n\n"
        "Rules:\n"
        "- Fix ONLY the reported bug. Do not refactor unrelated code.\n"
        "- Preserve all existing comments, docstrings, and formatting exactly.\n"
        "- If compile fails after a write, read the file back to inspect it before trying again.\n"
        "- After 2 failed write attempts, output FAILED: with a clear explanation.\n"
        "- Never modify smith.py.\n\n"
        "Your final message MUST start with 'FIXED:' or 'FAILED:' — no exceptions."
    ),
    "cache_control": {"type": "ephemeral"},
}]


def _make_result(status: str, finding: dict, message: str, patched_files: list) -> dict:
    return {
        "status":        status,
        "finding_id":    finding.get("id", "unknown"),
        "title":         finding.get("title", ""),
        "file":          finding.get("file", ""),
        "severity":      finding.get("severity", ""),
        "rank_score":    finding.get("rank_score", 0),
        "message":       message,
        "patched_files": patched_files,
    }


def phase2_fix(finding: dict) -> dict:
    """
    Sonnet engineers a fix for one finding via agentic tool loop.
    Returns a result dict with status (fixed | failed | cascade_failed).
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    user_msg = (
        f"Fix this bug in DEWD:\n\n"
        f"**File**: {finding['file']}\n"
        f"**Function**: {finding.get('function', 'module-level')}\n"
        f"**Line hint**: {finding.get('line_hint', 'unknown')}\n"
        f"**Title**: {finding['title']}\n"
        f"**Description**: {finding['description']}\n"
        f"**Fix hint**: {finding.get('fix_hint', 'none provided')}\n"
        f"**Rank score**: {finding.get('rank_score', 0)} "
        f"(severity: {finding.get('severity', '?')})\n\n"
        f"Read the file, fix the bug, compile-verify, then respond FIXED: or FAILED:."
    )

    messages      = [{"role": "user", "content": user_msg}]
    patched_files = []
    write_attempts = 0
    final_text     = ""

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=4000,
            system=_PHASE2_SYSTEM,
            tools=SMITH_TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    final_text = block.text
            break

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        abort = False
        for block in response.content:
            if block.type != "tool_use":
                continue

            log.info(f"    [smith/tool] {block.name}({list(block.input.keys())})")

            if block.name == "write_file":
                write_attempts += 1
                if write_attempts > MAX_ATTEMPTS_PER_FINDING:
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     f"ERROR: Max write attempts ({MAX_ATTEMPTS_PER_FINDING}) reached.",
                    })
                    final_text = f"FAILED: Exceeded {MAX_ATTEMPTS_PER_FINDING} write attempts."
                    abort = True
                    break
                patched_files.append(tool_input_path := block.input.get("path", ""))

            result_text = _execute_tool(block.name, block.input)
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     result_text,
            })

        messages.append({"role": "user", "content": tool_results})
        if abort:
            break

    success = final_text.startswith("FIXED:")

    if success and patched_files:
        for fpath in patched_files:
            ok, err = _run_cascade_check(fpath)
            if not ok:
                log.error(f"  [smith] cascade failure: {err}")
                _restore_backup(fpath)
                return _make_result("cascade_failed", finding,
                                    f"FAILED: Cascade error after patch — {err}", patched_files)

    status = "fixed" if success else "failed"
    return _make_result(status, finding, final_text or "No final message from engineer.", patched_files)


# ── Phase 3: Blueprint Builder ─────────────────────────────────────────────────

_DANGEROUS_CODE_PATTERNS = [
    (r"eval\s*\(",                          "eval() — code injection risk"),
    (r"exec\s*\(",                          "exec() — code injection risk"),
    (r"__import__\s*\(",                    "__import__() — dynamic import risk"),
    (r"os\.system\s*\(",                    "os.system() — shell injection risk"),
    (r"subprocess\b[^\n]*shell\s*=\s*True", "subprocess with shell=True — injection risk"),
    (r"(?<!\w)open\s*\([^)]*\.\.[/\\]",    "path traversal in open()"),
]


def _safety_scan(content: str) -> tuple:
    for pattern, reason in _DANGEROUS_CODE_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return False, reason
    return True, ""


def _get_existing_blueprint_ids() -> set:
    try:
        os.makedirs(BLUEPRINTS_DIR, exist_ok=True)
        return {
            f[:-5] for f in os.listdir(BLUEPRINTS_DIR)
            if f.endswith(".json")
        }
    except Exception:
        return set()


SMITH_PHASE3_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a DEWD project file. Path relative to project root or absolute.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (e.g. 'agents/frontier.py')"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "stage_file",
        "description": (
            "Stage your implementation for human review — does NOT go live until approved. "
            "Always write the complete file — never partial content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string", "description": "File path relative to project root"},
                "content": {"type": "string", "description": "Complete new file content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_py_compile",
        "description": "Syntax-check a Python file. Checks staged version if available.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to project root or absolute"}
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_project_files",
        "description": "List all .py source files in the DEWD project.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "grep_codebase",
        "description": "Search for a string or pattern across all DEWD .py files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search string or regex pattern"}
            },
            "required": ["pattern"],
        },
    },
]


_PHASE3_SYSTEM = [{
    "type": "text",
    "text": (
        "You are Smith Phase 3 — DEWD's autonomous builder. "
        "You have been given a feature specification. Implement it precisely in DEWD.\n\n"
        "Your output goes into a staging blueprint for human review — nothing goes live until approved.\n\n"
        "Process:\n"
        "1. read_file — understand the files you need to modify\n"
        "2. stage_file — write your complete implementation (staged, not live)\n"
        "3. run_py_compile — verify the staged file compiles cleanly\n"
        "4. Output your final message starting with exactly 'BUILT:' or 'FAILED:'\n\n"
        "Rules:\n"
        "- Implement ONLY what the spec describes. No extras, no refactoring.\n"
        "- Write minimal, clean code that matches DEWD's existing style exactly.\n"
        "- NEVER use eval(), exec(), os.system(), or subprocess with shell=True.\n"
        "- NEVER add outbound network calls to new domains.\n"
        "- Preserve all existing comments, docstrings, and formatting.\n"
        "- If you cannot implement safely and cleanly, output FAILED: with a clear explanation.\n\n"
        "Your final message MUST start with 'BUILT:' or 'FAILED:' — no exceptions."
    ),
    "cache_control": {"type": "ephemeral"},
}]


def phase3_build(opportunity: dict) -> dict | None:
    """
    Build a blueprint for one Frontier opportunity.
    Stages implementation for human review — does not deploy.
    Returns blueprint dict on success, None on failure.
    """
    spec         = opportunity.get("implementation_spec", {})
    blueprint_id = spec.get("blueprint_id", "").strip()
    if not blueprint_id:
        return None

    log.info(f"  [smith/phase3] building blueprint: {blueprint_id}")

    staged_files = {}   # rel_path -> content
    temp_dir     = tempfile.mkdtemp(prefix="dewd_bp_")

    def _exec_phase3(tool_name: str, tool_input: dict) -> str:
        if tool_name == "stage_file":
            path    = tool_input.get("path", "")
            content = tool_input.get("content", "")
            if not path or not content:
                return "ERROR: path and content are required"
            staged_files[path] = content
            temp_path = os.path.join(temp_dir, os.path.basename(path))
            try:
                with open(temp_path, "w") as f:
                    f.write(content)
                return f"OK: staged {path} ({len(content)} bytes)"
            except Exception as e:
                return f"ERROR: {e}"

        elif tool_name == "run_py_compile":
            path     = tool_input.get("path", "")
            rel      = os.path.relpath(_resolve_path(path), PROJECT_DIR)
            temp_path = os.path.join(temp_dir, os.path.basename(path))
            compile_target = temp_path if (rel in staged_files and os.path.exists(temp_path)) else _resolve_path(path)
            try:
                result = subprocess.run(
                    [PYTHON_BIN, "-m", "py_compile", compile_target],
                    capture_output=True, text=True, timeout=15, cwd=PROJECT_DIR,
                )
                return "OK" if result.returncode == 0 else (result.stderr.strip() or "Compile error")
            except Exception as e:
                return f"ERROR: {e}"

        else:
            return _execute_tool(tool_name, tool_input)

    client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    user_msg = (
        f"Implement this feature for DEWD:\n\n"
        f"**Opportunity**: {opportunity.get('name', '')}\n"
        f"**Summary**: {spec.get('summary', '')}\n"
        f"**Files to touch**: {', '.join(spec.get('files_to_touch', []))}\n"
        f"**What to build**: {spec.get('what_to_build', '')}\n"
        f"**Constraints**: {json.dumps(spec.get('constraints', []))}\n\n"
        f"Read the relevant files, stage your implementation, compile-verify, then respond BUILT: or FAILED:."
    )

    messages        = [{"role": "user", "content": user_msg}]
    final_text      = ""
    write_attempts  = 0

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=4000,
            system=_PHASE3_SYSTEM,
            tools=SMITH_PHASE3_TOOLS,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    final_text = block.text
            break

        if response.stop_reason != "tool_use":
            break

        tool_results = []
        abort = False
        for block in response.content:
            if block.type != "tool_use":
                continue
            log.info(f"    [smith/phase3/tool] {block.name}({list(block.input.keys())})")
            if block.name == "stage_file":
                write_attempts += 1
                if write_attempts > MAX_ATTEMPTS_PER_FINDING:
                    tool_results.append({
                        "type": "tool_result", "tool_use_id": block.id,
                        "content": f"ERROR: Max stage attempts reached.",
                    })
                    final_text = "FAILED: Exceeded max stage attempts."
                    abort = True
                    break
            result_text = _exec_phase3(block.name, block.input)
            tool_results.append({
                "type": "tool_result", "tool_use_id": block.id, "content": result_text,
            })

        messages.append({"role": "user", "content": tool_results})
        if abort:
            break

    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)

    if not final_text.startswith("BUILT:"):
        log.error(f"  [smith/phase3] blueprint {blueprint_id} failed: {final_text[:120]}")
        return None

    if not staged_files:
        log.error(f"  [smith/phase3] BUILT: but no files were staged for {blueprint_id}")
        return None

    # Safety scan every staged file
    for path, content in staged_files.items():
        ok, reason = _safety_scan(content)
        if not ok:
            log.error(f"  [smith/phase3] safety scan FAILED for {path}: {reason}")
            return None

    # Save blueprint
    os.makedirs(BLUEPRINTS_DIR, exist_ok=True)
    blueprint = {
        "id":               blueprint_id,
        "name":             opportunity.get("name", blueprint_id),
        "opportunity_name": opportunity.get("name", ""),
        "opportunity_why":  opportunity.get("why_dewd", ""),
        "score":            opportunity.get("score", 0),
        "summary":          spec.get("summary", ""),
        "files": [
            {"path": path, "content": content}
            for path, content in staged_files.items()
        ],
        "safety_passed":  True,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "status":         "pending_review",
    }
    bp_path = os.path.join(BLUEPRINTS_DIR, f"{blueprint_id}.json")
    from agents.common import atomic_write as _aw
    _aw(bp_path, blueprint)

    log.info(f"  [smith/phase3] blueprint saved: {blueprint_id} ({len(staged_files)} file(s))")
    return blueprint


def _final_import_test() -> tuple:
    """Verify dewd_web still imports cleanly after all patches."""
    try:
        result = subprocess.run(
            [PYTHON_BIN, "-c", "import dewd_web"],
            capture_output=True, text=True, timeout=20,
            cwd=PROJECT_DIR,
            env={**os.environ, "PYTHONPATH": PROJECT_DIR},
        )
        if result.returncode == 0:
            return True, "OK"
        return False, result.stderr.strip() or "Import failed (no output)"
    except subprocess.TimeoutExpired:
        return False, "Timed out"
    except Exception as e:
        return False, str(e)


def _append_log(result: dict, finding: dict):
    now_et = datetime.now(_ET)
    ts     = now_et.strftime("%Y-%m-%d %H:%M %Z")
    icon   = "FIXED" if result["status"] == "fixed" else "FAILED"

    entry = (
        f"\n---\n\n"
        f"**{ts}** | {icon}\n"
        f"- **File**: `{finding.get('file', '?')}`\n"
        f"- **Function**: `{finding.get('function', 'module-level')}`\n"
        f"- **Issue**: {finding.get('title', '')}\n"
        f"- **Severity**: {finding.get('severity', '?')} | "
        f"**Rank**: {finding.get('rank_score', 0)}\n"
        f"- **Result**: {result['message']}\n"
    )
    try:
        with open(SMITH_LOG_PATH, "a") as f:
            f.write(entry)
    except Exception as e:
        log.error(f"  [smith] log append failed: {e}")


def _is_morning_run() -> bool:
    now_et = datetime.now(_ET)
    return SMITH_BRIEF_WINDOW[0] <= now_et.hour < SMITH_BRIEF_WINDOW[1]


def _build_morning_brief(fixed: list, failed: list, import_ok: bool) -> str:
    lines = [f"Good morning, {OWNER_NAME}. DEWD morning brief.\n\n"]

    try:
        with open(os.path.join(AGENTS_DIR, "daymark.json")) as f:
            daymark = json.load(f)
        report = daymark.get("report", "")
        sections = report.split("##")
        for s in sections[1:3]:
            lines.append("##" + s.strip() + "\n\n")
    except Exception:
        lines.append("## WORLD\nDaymark data unavailable.\n\n")

    lines.append("## FRONTIER PICKS\n")
    try:
        with open(os.path.join(AGENTS_DIR, "frontier.json")) as f:
            frontier = json.load(f)
        opps = frontier.get("opportunities", [])[:3]
        if opps:
            for opp in opps:
                lines.append(f"- **{opp['name']}** [{opp['score']}/17] — {opp['why_dewd']}\n")
        else:
            lines.append("Nothing new above threshold.\n")
        pkg_updates = frontier.get("package_updates", [])
        if pkg_updates:
            pkgs = ", ".join(p["package"] for p in pkg_updates)
            lines.append(f"\n{len(pkg_updates)} package update(s): {pkgs}\n")
    except Exception:
        lines.append("Frontier data unavailable.\n")

    lines.append("\n## SMITH\n")
    if fixed:
        lines.append(f"{len(fixed)} fix(es) shipped:\n")
        for r in fixed:
            lines.append(f"- `{r['file']}` — {r['title']}\n")
    else:
        lines.append("No bugs found or fixed this run.\n")
    if failed:
        lines.append(f"\n{len(failed)} finding(s) unresolved:\n")
        for r in failed:
            lines.append(f"- `{r['file']}` — {r['title']} ({r['status']})\n")

    import_status = "PASS" if import_ok else "FAIL — check smith.json"
    lines.append(f"\nImport test: **{import_status}**\n")

    try:
        temp_r = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True, text=True, timeout=5,
        )
        temp = temp_r.stdout.strip() if temp_r.returncode == 0 else "unavailable"
    except Exception:
        temp = "unavailable"
    lines.append(f"Pi temp: {temp}\n")

    return "".join(lines)


def _run_phase3() -> list:
    """
    Check frontier.json for implementable opportunities and build blueprints.
    Returns list of blueprint ids that were created this run.
    """
    frontier_file = os.path.join(AGENTS_DIR, "frontier.json")
    created = []
    try:
        if not os.path.exists(frontier_file):
            return created
        with open(frontier_file) as f:
            frontier_data = json.load(f)
        existing_ids = _get_existing_blueprint_ids()
        for opp in frontier_data.get("opportunities", []):
            if not opp.get("implement"):
                continue
            spec = opp.get("implementation_spec", {})
            bid  = spec.get("blueprint_id", "").strip()
            if not bid or bid in existing_ids:
                continue
            log.info(f"  [smith/phase3] new blueprint candidate: {bid}")
            bp = phase3_build(opp)
            if bp:
                created.append(bp)
                existing_ids.add(bid)
    except Exception as e:
        log.error(f"  [smith/phase3] phase3 check failed: {e}")
    return created


def run() -> dict:
    os.makedirs(AGENTS_DIR, exist_ok=True)
    write_status(OUTPUT_FILE, "running")
    started_at     = datetime.now(timezone.utc).isoformat()
    fixed_results  = []
    failed_results = []
    import_ok      = True
    import_err     = "skipped — no changes"

    try:
        log.info("  [smith] scanning project files…")
        source_files = _scan_project_files()
        seen         = _load_seen()

        log.info(f"  [smith] phase 1 — auditing {len(source_files)} files with Haiku…")
        findings = phase1_audit(source_files, seen)
        log.info(f"  [smith] phase 1 complete — {len(findings)} finding(s)")

        if findings:
            for i, finding in enumerate(findings, 1):
                log.info(f"  [smith] phase 2 [{i}/{len(findings)}] — {finding['title']}")
                fix_result = phase2_fix(finding)

                if fix_result["status"] == "fixed":
                    fixed_results.append(fix_result)
                    _mark_bug_seen(seen, finding, "fixed")
                    log.info(f"  [smith]   ✓ {finding['title']}")
                else:
                    failed_results.append(fix_result)
                    _mark_bug_seen(seen, finding, fix_result["status"])
                    log.error(f"  [smith]   ✗ {finding['title']} ({fix_result['status']})")

                _append_log(fix_result, finding)
                time.sleep(1)

            _update_fingerprints(_scan_project_files(), seen)
            log.info("  [smith] running final import test…")
            import_ok, import_err = _final_import_test()
            log.info(f"  [smith] import test: {'✓ PASS' if import_ok else '✗ FAIL — ' + import_err}")
        else:
            _update_fingerprints(source_files, seen)

        # Phase 3 — blueprint builder (runs regardless of Phase 1/2 outcome)
        log.info("  [smith] phase 3 — checking Frontier for blueprint candidates…")
        new_blueprints = _run_phase3()
        for bp in new_blueprints:
            send_alert(
                f"Blueprint Staged — {bp['name']}",
                f"Smith has staged a blueprint for your review, Sir.\n\n"
                f"Opportunity: {bp['opportunity_name']}\n"
                f"Summary: {bp['summary']}\n"
                f"Files: {', '.join(f['path'] for f in bp['files'])}\n\n"
                f"To deploy, tell me: implement blueprint {bp['id']}",
            )

        brief = ""
        if _is_morning_run():
            log.info("  [smith] building morning brief…")
            brief = _build_morning_brief(fixed_results, failed_results, import_ok)
            date_str = datetime.now(_ET).strftime("%b %d")
            send_alert(f"DEWD Morning Brief — {date_str}", brief[:4000])
        elif fixed_results:
            files = ", ".join(r["file"] for r in fixed_results[:3])
            send_alert(f"Smith — {len(fixed_results)} fix(es)", f"Patched: {files}")

        result = {
            "status":      "ok",
            "ran_at":      started_at,
            "findings":    len(findings),
            "fixed":       fixed_results,
            "failed":      failed_results,
            "import_test": "pass" if import_ok else f"fail: {import_err}",
            "brief":       brief,
        }

    except Exception as e:
        log.error(f"  [smith] run failed: {e}")
        send_alert("Smith Error", str(e), priority="high")
        result = {
            "status":      "error",
            "ran_at":      started_at,
            "error":       str(e),
            "findings":    0,
            "fixed":       [],
            "failed":      [],
            "import_test": "skipped",
            "brief":       "",
        }

    atomic_write(OUTPUT_FILE, result)
    return result


def stream_run():
    """Generator — yields progress dicts for SSE streaming."""
    os.makedirs(AGENTS_DIR, exist_ok=True)
    write_status(OUTPUT_FILE, "running")
    started_at = datetime.now(timezone.utc).isoformat()
    fixed_results  = []
    failed_results = []

    try:
        yield {"msg": "Scanning project files…"}
        source_files = _scan_project_files()
        seen         = _load_seen()

        yield {"msg": f"Phase 1 — auditing {len(source_files)} files…"}
        findings = phase1_audit(source_files, seen)

        if not findings:
            _update_fingerprints(source_files, seen)
            yield {"msg": "No changed files to audit. Smith is done."}
            atomic_write(OUTPUT_FILE, {
                "status": "ok", "ran_at": started_at,
                "findings": 0, "fixed": [], "failed": [],
                "import_test": "skipped", "brief": "",
            })
            return

        yield {"msg": f"Phase 1 found {len(findings)} finding(s). Starting Phase 2…"}

        for i, finding in enumerate(findings, 1):
            yield {"msg": f"[{i}/{len(findings)}] Fixing: {finding['title']}"}
            fix_result = phase2_fix(finding)

            if fix_result["status"] == "fixed":
                fixed_results.append(fix_result)
                _mark_bug_seen(seen, finding, "fixed")
                yield {"msg": f"✓ Fixed: {finding['title']}"}
            else:
                failed_results.append(fix_result)
                _mark_bug_seen(seen, finding, fix_result["status"])
                yield {"msg": f"✗ Could not fix: {finding['title']} ({fix_result['status']})"}

            _append_log(fix_result, finding)
            time.sleep(1)

        _update_fingerprints(_scan_project_files(), seen)

        yield {"msg": "Running final import test…"}
        import_ok, import_err = _final_import_test()
        yield {"msg": f"Import test: {'PASS' if import_ok else 'FAIL — ' + import_err}"}

        brief = ""
        if _is_morning_run():
            yield {"msg": "Building morning brief…"}
            brief = _build_morning_brief(fixed_results, failed_results, import_ok)
            date_str = datetime.now(_ET).strftime("%b %d")
            send_alert(f"DEWD Morning Brief — {date_str}", brief[:4000])
        elif fixed_results:
            files = ", ".join(r["file"] for r in fixed_results[:3])
            send_alert(f"Smith — {len(fixed_results)} fix(es)", f"Patched: {files}")

        atomic_write(OUTPUT_FILE, {
            "status":      "ok",
            "ran_at":      started_at,
            "findings":    len(findings),
            "fixed":       fixed_results,
            "failed":      failed_results,
            "import_test": "pass" if import_ok else f"fail: {import_err}",
            "brief":       brief,
        })

    except Exception as e:
        write_error(OUTPUT_FILE, e)
        send_alert("Smith Error", str(e), priority="high")
        yield {"error": str(e)}


if __name__ == "__main__":
    r = run()
    log.error(f"\nFindings: {r['findings']} | Fixed: {len(r['fixed'])} | "
          f"Failed: {len(r['failed'])} | Import: {r['import_test']}")
    for item in r["fixed"]:
        log.info(f"  ✓ [{item['rank_score']}] {item['title']}")
    for item in r["failed"]:
        log.error(f"  ✗ [{item['rank_score']}] {item['title']} ({item['status']})")
