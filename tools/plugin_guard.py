#!/usr/bin/env python3
"""Plugin Guard — ``skills_guard`` engine applied to ``hermes plugins install``/``update``.

Plugins run in-process but are *expected* to read their own env keys, call provider APIs
and spawn subprocesses, so: full pattern set on docs/config files (where prompt-injection
lives); the "reads own secret"/"HTTP call with key" family exempt on *code* files;
plugin-sized structural limits; VCS/venv noise skipped. ``safe`` installs, ``caution``
needs confirmation, ``dangerous`` is blocked and ``--force`` does NOT override.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from tools.skills_guard import (
    Finding, ScanResult, SCANNABLE_EXTENSIONS, SUSPICIOUS_BINARY_EXTENSIONS,
    _determine_verdict, format_scan_report,
    scan_file)

PLUGIN_SCANNER_VERSION = "plugin-guard-v2"

# Never scanned: VCS internals, caches, vendored envs.
EXCLUDED_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox"}

# Code files, where "reads an env secret" / "HTTP call with a key" is normal (requires_env).
CODE_FILE_EXTENSIONS = {".py", ".js", ".ts", ".sh", ".bash", ".rb", ".pl", ".php"}

# Pattern ids exempt on code files (every legitimate provider plugin trips them); still
# applied in full to docs/config files.
CODE_EXEMPT_PATTERN_IDS = {
    "python_environ_get_secret", "python_getenv_secret", "python_os_environ", "node_process_env",
    "ruby_env_secret", "env_exfil_httpx", "env_exfil_requests", "env_exfil_fetch",
    "env_exfil_curl", "env_exfil_wget",
    # Agent-facing instruction patterns are meaningless inside code (prompt docstrings trip them).
    "context_exfil", "send_to_url", "fake_policy",
    # Plugins legitimately write config.yaml in post_setup and base64 credentials (Basic auth).
    "agent_config_mod", "agent_config_contract", "encoded_exfil"}

# Severity remaps: a bundled binary is warn-tier (repos occasionally vendor one); a mere
# ``~/.hermes/.env`` mention is how READMEs say where keys go (READING it still trips
# ``read_secrets_file``, critical); ``curl | sh`` in READMEs is caution, not a hard block.
SEVERITY_REMAP = {
    "binary_file": "high", "hermes_env_access": "medium", "curl_pipe_shell": "high"}

# Structural limits — plugins are real codebases, far larger than skills.
MAX_PLUGIN_FILE_COUNT = 400
MAX_PLUGIN_TOTAL_SIZE_KB = 10 * 1024   # 10MB of scannable tree
MAX_PLUGIN_SINGLE_FILE_KB = 1024       # 1MB single file


def _walk(plugin_dir: Path) -> Iterator[Tuple[Path, str]]:
    """Yield (path, "a/b/c" relative path) for every non-excluded entry under plugin_dir."""
    for f in plugin_dir.rglob("*"):
        try:
            rel_parts = f.relative_to(plugin_dir).parts
        except ValueError:
            continue
        if not any(part in EXCLUDED_DIRS for part in rel_parts):
            yield f, "/".join(rel_parts)


def _finding(pattern_id: str, severity: str, category: str, file: str, match: str, description: str) -> Finding:
    return Finding(pattern_id, severity, category, file, 0, match, description)


_CONTAINMENT_ASSERTION = re.compile(
    r"(?P<quote>`?)[\w./-]*/etc/(?:passwd|shadow)(?P=quote)\s+"
    r"must not escape [\w-]+(?: dir(?:ectory)?)?\.?", re.IGNORECASE)


def _is_containment_reference(line: str, suffix: str) -> bool:
    """Only complete path-containment assertions in Python comments / Markdown table cells.

    Keep these at caution, not safe: prose is not proof. Never exempt a test directory,
    executable expression, or a second password-file reference elsewhere on the line.
    """
    text = line.strip()
    if suffix == ".py" and text.startswith("#"):
        parts = text[1:].strip().split(". ")
    elif suffix == ".md" and text.startswith("|") and text.endswith("|"):
        parts = text[1:-1].split("|")
    else:
        return False
    remaining = [part for part in parts if not _CONTAINMENT_ASSERTION.fullmatch(part.strip())]
    return len(remaining) < len(parts) and not re.search(
        r"/etc/passwd|/etc/shadow", " ".join(remaining), re.IGNORECASE)


def _filter_findings(findings: List[Finding], rel_path: str, lines: List[str]) -> List[Finding]:
    """Apply plugin-specific exemptions and severity remaps to raw findings."""
    is_code = Path(rel_path).suffix.lower() in CODE_FILE_EXTENSIONS

    out: List[Finding] = []
    for f in findings:
        if is_code and f.pattern_id in CODE_EXEMPT_PATTERN_IDS:
            continue
        f.severity = SEVERITY_REMAP.get(f.pattern_id) or f.severity
        if (f.pattern_id == "system_passwd_access" and 0 < f.line <= len(lines)
                and _is_containment_reference(lines[f.line - 1], Path(rel_path).suffix.lower())):
            f.severity = "high"
            f.description = "documents a path-containment assertion (verify intent)"
        out.append(f)
    return out


def _check_plugin_structure(plugin_dir: Path) -> List[Finding]:
    """Structural checks sized for plugin repositories."""
    findings: List[Finding] = []
    file_count = 0
    total_size = 0
    resolved_root = plugin_dir.resolve()
    for f, rel in _walk(plugin_dir):
        if f.is_symlink():
            file_count += 1
            try:
                resolved = f.resolve()
            except OSError:
                findings.append(_finding("broken_symlink", "medium", "traversal", rel,
                                         "broken symlink", "broken or circular symlink"))
                continue
            if not resolved.is_relative_to(resolved_root):
                findings.append(_finding("symlink_escape", "critical", "traversal", rel,
                                         f"symlink -> {resolved}", "symlink points outside the plugin directory"))
            continue
        if not f.is_file():
            continue
        file_count += 1
        try:
            size = f.stat().st_size
        except OSError:
            continue
        total_size += size
        if size > MAX_PLUGIN_SINGLE_FILE_KB * 1024:
            findings.append(_finding("oversized_file", "medium", "structural", rel, f"{size // 1024}KB",
                                     f"file is {size // 1024}KB (limit: {MAX_PLUGIN_SINGLE_FILE_KB}KB)"))
        ext = f.suffix.lower()
        if ext in SUSPICIOUS_BINARY_EXTENSIONS:
            findings.append(_finding("binary_file", SEVERITY_REMAP["binary_file"], "structural", rel,
                                     f"binary: {ext}", f"binary/executable file ({ext}) bundled in plugin (cannot be scanned)"))
    if file_count > MAX_PLUGIN_FILE_COUNT:
        findings.append(_finding("too_many_files", "medium", "structural", "(directory)", f"{file_count} files",
                                 f"plugin has {file_count} files (limit: {MAX_PLUGIN_FILE_COUNT})"))
    if total_size > MAX_PLUGIN_TOTAL_SIZE_KB * 1024:
        findings.append(_finding("oversized_bundle", "medium", "structural", "(directory)", f"{total_size // 1024}KB",
                                 f"plugin is {total_size // 1024}KB total (limit: {MAX_PLUGIN_TOTAL_SIZE_KB}KB)"))
    return findings


def scan_plugin(plugin_dir: Path, source: str = "") -> ScanResult:
    """Scan a plugin directory (typically the temp clone); every external plugin is ``community`` trust."""
    all_findings: List[Finding] = []
    if plugin_dir.is_dir():
        all_findings.extend(_check_plugin_structure(plugin_dir))
        for f, rel in sorted(_walk(plugin_dir)):
            if f.is_file() and not f.is_symlink() and f.suffix.lower() in SCANNABLE_EXTENSIONS:
                try:
                    content = f.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                # Use one snapshot, not a second read or the truncated Finding.match.
                findings = scan_file(f, rel_path=rel, content=content)
                all_findings.extend(_filter_findings(findings, rel, content.split("\n")))
    verdict = _determine_verdict(all_findings)
    if all_findings:
        categories = sorted({f.category for f in all_findings})
        summary = f"{plugin_dir.name}: {verdict} — {len(all_findings)} finding(s) in {', '.join(categories)}"
    else:
        summary = f"{plugin_dir.name}: clean scan, no threats detected"
    result = ScanResult(
        skill_name=plugin_dir.name, source=source or plugin_dir.name, trust_level="community",
        verdict=verdict, findings=all_findings, scanned_at=datetime.now(timezone.utc).isoformat(),
        summary=summary)
    result.scan_provenance = {
        "scanner_version": PLUGIN_SCANNER_VERSION, "verdict": verdict, "source": result.source}
    return result


def should_allow_plugin_install(
    result: ScanResult, force: bool = False) -> Tuple[Optional[bool], str]:
    """Map a verdict to ``(allowed, reason)``: True installs, None asks to confirm, False blocks."""
    n = len(result.findings)
    if result.verdict == "safe":
        return True, "Allowed (clean scan)"
    if result.verdict == "caution":
        if force:
            return True, f"Force-installed despite caution verdict ({n} findings)"
        return None, f"Requires confirmation (caution verdict, {n} findings)"
    return False, (
        f"Blocked (dangerous verdict, {n} findings). "
        f"--force does not override a dangerous verdict.")


__all__ = [
    "scan_plugin", "should_allow_plugin_install", "format_scan_report", "PLUGIN_SCANNER_VERSION"]
