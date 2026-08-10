"""Tool result persistence -- preserves large outputs instead of truncating.

Defense against context-window overflow operates at three levels:

1. **Per-tool output cap** (inside each tool): Tools like search_files
   pre-truncate their own output before returning. This is the first line
   of defense and the only one the tool author controls.

2. **Per-result persistence** (maybe_persist_tool_result): After a tool
   returns, if its output exceeds the tool's registered threshold
   (registry.get_max_result_size), the full output is written INTO THE
   SANDBOX temp dir (for example /tmp/hermes-results/{tool_use_id}.txt on
   standard Linux, or $TMPDIR/hermes-results/{tool_use_id}.txt on Termux)
   via env.execute(). The in-context content is replaced with a preview +
   file path reference. The model can read_file to access the full output
   on any backend.

3. **Per-turn aggregate budget** (enforce_turn_budget): After all tool
   results in a single assistant turn are collected, if the total exceeds
   MAX_TURN_BUDGET_CHARS (200K), the largest non-persisted results are
   spilled to disk until the aggregate is under budget. This catches cases
   where many medium-sized results combine to overflow context.
"""

import hashlib
import logging
import os
import re
import shlex
import tempfile
import time
import uuid
from pathlib import Path

from tools.budget_config import (
    DEFAULT_PREVIEW_SIZE_CHARS,
    BudgetConfig,
    DEFAULT_BUDGET,
)

logger = logging.getLogger(__name__)
PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"
STORAGE_DIR = "/tmp/hermes-results"
LOCAL_STORAGE_DIR = Path(tempfile.gettempdir()) / "hermes-results"
SPILL_RETENTION_SECONDS = 7 * 24 * 60 * 60
HARD_RESULT_SIZE_CHARS = 100_000
HEREDOC_MARKER = "HERMES_PERSIST_EOF"
_BUDGET_TOOL_NAME = "__budget_enforcement__"
_UNSAFE_RESULT_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_RESULT_FILENAME_STEM = 120


def cleanup_local_spills(*, now: float | None = None) -> int:
    """Delete managed local spill files older than seven days."""
    cutoff = (time.time() if now is None else now) - SPILL_RETENTION_SECONDS
    removed = 0
    try:
        LOCAL_STORAGE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(LOCAL_STORAGE_DIR, 0o700)
        for path in LOCAL_STORAGE_DIR.iterdir():
            if not path.is_file() or path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
    except OSError:
        logger.debug("Could not clean local tool-result spills", exc_info=True)
    return removed


class AtomicSpillWriter:
    """Stream UTF-8 text to a private temp file, then atomically publish it."""

    def __init__(self, filename: str):
        cleanup_local_spills()
        self.path = LOCAL_STORAGE_DIR / _safe_result_filename(filename)
        fd, tmp = tempfile.mkstemp(
            dir=LOCAL_STORAGE_DIR,
            prefix=f".{self.path.stem}.",
            suffix=".tmp",
        )
        os.chmod(tmp, 0o600)
        self._tmp = Path(tmp)
        self._file = os.fdopen(fd, "w", encoding="utf-8", newline="")

    def write(self, content: str) -> None:
        if not isinstance(content, str):
            raise TypeError("tool-result spill accepts text only")
        self._file.write(content)

    def commit(self) -> str:
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        os.replace(self._tmp, self.path)
        os.chmod(self.path, 0o600)
        return str(self.path)

    def abort(self) -> None:
        try:
            self._file.close()
        finally:
            try:
                self._tmp.unlink()
            except OSError:
                pass


def _resolve_storage_dir(env) -> str:
    """Return the best temp-backed storage dir for this environment."""
    if env is not None:
        get_temp_dir = getattr(env, "get_temp_dir", None)
        if callable(get_temp_dir):
            try:
                temp_dir = get_temp_dir()
            except Exception as exc:
                logger.debug("Could not resolve env temp dir: %s", exc)
            else:
                if temp_dir:
                    temp_dir = temp_dir.rstrip("/") or "/"
                    return f"{temp_dir}/hermes-results"
    return STORAGE_DIR


def _safe_result_filename(tool_use_id: str) -> str:
    """Return a single safe filename for a tool result id."""
    raw_id = str(tool_use_id or "tool_result")
    safe_stem = _UNSAFE_RESULT_FILENAME_CHARS.sub("_", raw_id).strip("._-")
    changed = safe_stem != raw_id

    if not safe_stem:
        safe_stem = "tool_result"
        changed = True

    if changed or len(safe_stem) > _MAX_RESULT_FILENAME_STEM:
        digest = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:12]
        safe_stem = safe_stem[:_MAX_RESULT_FILENAME_STEM].rstrip("._-") or "tool_result"
        safe_stem = f"{safe_stem}_{digest}"

    return f"{safe_stem}.txt"


def generate_preview(content: str, max_chars: int = DEFAULT_PREVIEW_SIZE_CHARS) -> tuple[str, bool]:
    """Truncate at last newline within max_chars. Returns (preview, has_more)."""
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[:last_nl + 1]
    return truncated, True


def _heredoc_marker(content: str) -> str:
    """Return a heredoc delimiter that doesn't collide with content."""
    if HEREDOC_MARKER not in content:
        return HEREDOC_MARKER
    return f"HERMES_PERSIST_{uuid.uuid4().hex[:8]}"


def _write_to_sandbox(content: str, remote_path: str, env) -> bool:
    """Write content into the sandbox via env.execute(). Returns True on success.

    Pushes ``content`` through stdin rather than embedding it in the command
    string. Linux's ``MAX_ARG_STRLEN`` caps any single argv element at 128 KB
    (32 * PAGE_SIZE), so the previous heredoc-in-the-command-string approach
    silently failed with ``OSError: [Errno 7] Argument list too long`` for any
    tool result over ~128 KB — exactly the case persistence exists to handle.
    Routing through stdin removes that ceiling on local + ssh (``_stdin_mode
    == "pipe"``); remote backends with ``_stdin_mode == "heredoc"`` keep their
    existing API-body sized limit, which is orders of magnitude larger than
    the exec-arg ceiling.
    """
    if not isinstance(content, str):
        raise TypeError("tool-result spill accepts text only")
    storage_dir = os.path.dirname(remote_path)
    temp_path = f"{remote_path}.tmp.{uuid.uuid4().hex}"
    quoted_dir = shlex.quote(storage_dir)
    quoted_temp = shlex.quote(temp_path)
    quoted_path = shlex.quote(remote_path)
    cmd = (
        f"umask 077; mkdir -p {quoted_dir} && "
        f"{{ find {quoted_dir} -type f -mmin +10080 -delete 2>/dev/null || true; }} && "
        f"cat > {quoted_temp} && chmod 600 {quoted_temp} && "
        f"mv -f {quoted_temp} {quoted_path}"
    )
    result = env.execute(cmd, timeout=30, stdin_data=content)
    return result.get("returncode", 1) == 0


def _build_persisted_message(
    preview: str,
    has_more: bool,
    original_size: int,
    file_path: str,
) -> str:
    """Build the <persisted-output> replacement block."""
    size_kb = original_size / 1024
    if size_kb >= 1024:
        size_str = f"{size_kb / 1024:.1f} MB"
    else:
        size_str = f"{size_kb:.1f} KB"

    msg = f"{PERSISTED_OUTPUT_TAG}\n"
    msg += f"This tool result was too large ({original_size:,} characters, {size_str}).\n"
    msg += f"Full output saved to: {file_path}\n"
    msg += "Use the read_file tool with offset and limit to access specific sections of this output.\n\n"
    msg += f"Preview (first {len(preview)} chars):\n"
    msg += preview
    if has_more:
        msg += "\n..."
    msg += f"\n{PERSISTED_OUTPUT_CLOSING_TAG}"
    return msg


def maybe_persist_tool_result(
    content: str,
    tool_name: str,
    tool_use_id: str,
    env=None,
    config: BudgetConfig = DEFAULT_BUDGET,
    threshold: int | float | None = None,
) -> str:
    """Layer 2: persist oversized result into the sandbox, return preview + path.

    Writes via env.execute() so the file is accessible from any backend
    (local, Docker, SSH, Modal, Daytona). Falls back to inline truncation
    if write fails or no env is available.

    Args:
        content: Raw tool result string.
        tool_name: Name of the tool (used for threshold lookup).
        tool_use_id: Unique ID for this tool call (used as filename).
        env: The active BaseEnvironment instance, or None.
        config: BudgetConfig controlling thresholds and preview size.
        threshold: Explicit override; takes precedence over config resolution.

    Returns:
        Original content if small, or <persisted-output> replacement.
    """
    if not isinstance(content, str):
        raise TypeError("tool result must be text")
    configured_threshold = threshold if threshold is not None else config.resolve_threshold(tool_name)
    effective_threshold = min(configured_threshold, HARD_RESULT_SIZE_CHARS)

    if len(content) <= effective_threshold:
        _log_result_sizes(tool_name, len(content), len(content), persisted=False)
        return content

    storage_dir = _resolve_storage_dir(env)
    remote_path = f"{storage_dir}/{_safe_result_filename(tool_use_id)}"
    preview, has_more = generate_preview(content, max_chars=config.preview_size)

    # Persisting a read_file result creates an unbounded persist/read loop.
    # Its head-tail fallback retains pagination metadata without another file.
    if env is not None and tool_name != "read_file":
        try:
            if _write_to_sandbox(content, remote_path, env):
                logger.info(
                    "Persisted large tool result: %s (%s, %d chars -> %s)",
                    tool_name, tool_use_id, len(content), remote_path,
                )
                replacement = _build_persisted_message(preview, has_more, len(content), remote_path)
                replacement = _hard_cap_replacement(replacement)
                _log_result_sizes(tool_name, len(content), len(replacement), persisted=True)
                return replacement
        except Exception as exc:
            logger.warning("Sandbox write failed for %s: %s", tool_use_id, exc)

    logger.info(
        "Inline-truncating large tool result: %s (%d chars, no sandbox write)",
        tool_name, len(content),
    )
    replacement = (
        f"{preview}\n\n"
        f"[Truncated: tool response was {len(content):,} chars. "
        f"Full output could not be saved to sandbox.]"
    )
    replacement = _hard_cap_replacement(replacement)
    _log_result_sizes(tool_name, len(content), len(replacement), persisted=False)
    return replacement


def _hard_cap_replacement(content: str) -> str:
    if len(content) <= HARD_RESULT_SIZE_CHARS:
        return content
    return generate_preview(content, max_chars=HARD_RESULT_SIZE_CHARS)[0]


def _log_result_sizes(tool_name: str, full_chars: int, returned_chars: int, *, persisted: bool) -> None:
    """Emit size-only telemetry; never log result content."""
    ratio = returned_chars / full_chars if full_chars else 1.0
    logger.info(
        "tool_result_size tool=%s full_chars=%d returned_chars=%d ratio=%.4f persisted=%s",
        tool_name,
        full_chars,
        returned_chars,
        ratio,
        persisted,
    )


def enforce_turn_budget(
    tool_messages: list[dict],
    env=None,
    config: BudgetConfig = DEFAULT_BUDGET,
) -> list[dict]:
    """Layer 3: enforce aggregate budget across all tool results in a turn.

    If total chars exceed budget, persist the largest non-persisted results
    first (via sandbox write) until under budget. Already-persisted results
    are skipped.

    Mutates the list in-place and returns it.
    """
    candidates = []
    total_size = 0
    for i, msg in enumerate(tool_messages):
        content = msg.get("content", "")
        size = len(content)
        total_size += size
        if PERSISTED_OUTPUT_TAG not in content:
            candidates.append((i, size))

    if total_size <= config.turn_budget:
        return tool_messages

    candidates.sort(key=lambda x: x[1], reverse=True)

    for idx, size in candidates:
        if total_size <= config.turn_budget:
            break
        msg = tool_messages[idx]
        content = msg["content"]
        tool_use_id = msg.get("tool_call_id", f"budget_{idx}")

        replacement = maybe_persist_tool_result(
            content=content,
            tool_name=_BUDGET_TOOL_NAME,
            tool_use_id=tool_use_id,
            env=env,
            config=config,
            threshold=0,
        )
        if replacement != content:
            total_size -= size
            total_size += len(replacement)
            tool_messages[idx]["content"] = replacement
            logger.info(
                "Budget enforcement: persisted tool result %s (%d chars)",
                tool_use_id, size,
            )

    return tool_messages
