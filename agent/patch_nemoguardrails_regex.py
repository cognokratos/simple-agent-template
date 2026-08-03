"""Backport NeMo Guardrails 0.21 streaming action fixes.

The pinned release needs three small compatibility patches:
- Regex actions must accept dispatcher-injected keyword arguments.
- Regex matches need an output mapping so they block a stream.
- Presidio's ``mask_sensitive_data`` action must also accept the keyword
  arguments injected by the streaming action dispatcher.

The script is idempotent and validates every modification.
"""

from pathlib import Path

from nemoguardrails.library.regex import actions as regex_actions
from nemoguardrails.library.sensitive_data_detection import actions as sensitive_actions


def patch_regex() -> Path:
    path = Path(regex_actions.__file__)
    source = path.read_text(encoding="utf-8")

    if "def _regex_blocked_mapping" not in source:
        marker = "\n\n@action(is_system_action=True)\nasync def detect_regex_pattern("
        replacement = (
            "\n\ndef _regex_blocked_mapping(result: RegexDetectionResult) -> bool:\n"
            "    \"\"\"Return True when a forbidden regex pattern matched.\"\"\"\n"
            "    return result.get(\"is_match\", False)\n"
            "\n\n@action(is_system_action=True, output_mapping=_regex_blocked_mapping)\n"
            "async def detect_regex_pattern("
        )
        if marker not in source:
            raise RuntimeError(f"Could not find regex action decorator in {path}")
        source = source.replace(marker, replacement, 1)
    elif "output_mapping=_regex_blocked_mapping" not in source:
        source = source.replace(
            "@action(is_system_action=True)\nasync def detect_regex_pattern(",
            "@action(is_system_action=True, output_mapping=_regex_blocked_mapping)\n"
            "async def detect_regex_pattern(",
            1,
        )

    signature = "    config: RailsConfig,\n) -> RegexDetectionResult:"
    if signature in source:
        source = source.replace(
            signature,
            "    config: RailsConfig,\n    **kwargs,\n) -> RegexDetectionResult:",
            1,
        )
    elif "    **kwargs,\n) -> RegexDetectionResult:" not in source:
        raise RuntimeError(f"Could not patch regex action signature in {path}")

    path.write_text(source, encoding="utf-8")

    patched = path.read_text(encoding="utf-8")
    required = (
        "output_mapping=_regex_blocked_mapping",
        "    **kwargs,\n) -> RegexDetectionResult:",
    )
    missing = [value for value in required if value not in patched]
    if missing:
        raise RuntimeError(f"Regex patch validation failed for {path}: {missing}")
    return path


def patch_presidio() -> Path:
    path = Path(sensitive_actions.__file__)
    source = path.read_text(encoding="utf-8")

    old = (
        "async def mask_sensitive_data(source: str, text: str, "
        "config: RailsConfig):"
    )
    new = (
        "async def mask_sensitive_data(\n"
        "    source: str,\n"
        "    text: str,\n"
        "    config: RailsConfig,\n"
        "    **kwargs,\n"
        "):"
    )

    if old in source:
        source = source.replace(old, new, 1)
    elif new not in source:
        raise RuntimeError(f"Could not patch Presidio action signature in {path}")

    path.write_text(source, encoding="utf-8")
    patched = path.read_text(encoding="utf-8")
    if new not in patched:
        raise RuntimeError(f"Presidio patch validation failed for {path}")
    return path


def main() -> None:
    regex_path = patch_regex()
    presidio_path = patch_presidio()
    print(f"Patched NeMo Guardrails regex streaming action: {regex_path}")
    print(f"Patched NeMo Guardrails Presidio streaming action: {presidio_path}")


if __name__ == "__main__":
    main()
