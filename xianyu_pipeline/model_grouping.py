"""Conservative grouping of archive filenames that belong to one model."""
from __future__ import annotations

from pathlib import Path
import re


_RATIO_RE = re.compile(
    r"(?<![A-Za-z0-9])(1\s*[-:xX×/]\s*\d{1,3})(?:\s*scale)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_PART_RE = re.compile(r"(?:[._\-\s]+part[._\-\s]*\d+)$", re.IGNORECASE)
_SUPPORTED_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:pre[\s._-]+supported|supported[\s._-]+pre)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"(?<![A-Za-z0-9])(?:pre|supported)(?![A-Za-z0-9])", re.IGNORECASE)


def _clean_display_name(value: str) -> str:
    value = re.sub(r"[_\s]+", " ", value)
    return value.strip(" ._-")


def model_group_info(filename: str) -> tuple[str, str, str, list[str]]:
    """Return key, display name, variant label, and normalized variant tags.

    The grouping intentionally removes only common scale and support markers.
    It does not perform fuzzy matching, so distinct model names remain separate.
    """
    raw_stem = Path(str(filename)).stem.strip()
    stem = _PART_RE.sub("", raw_stem).strip(" ._-")
    tags: list[str] = []

    def ratio_replacement(match: re.Match[str]) -> str:
        ratio = re.sub(r"\s+", "", match.group(1)).replace("-", ":").replace("x", ":").replace("X", ":").replace("×", ":").replace("/", ":")
        tags.append(ratio)
        return " "

    stem = _RATIO_RE.sub(ratio_replacement, stem)
    if _SUPPORTED_RE.search(stem):
        tags.append("预支撑")
        stem = _SUPPORTED_RE.sub(" ", stem)
    else:
        support_match = _TAG_RE.search(stem)
        if support_match:
            tags.append("预支撑")
            stem = _TAG_RE.sub(" ", stem)

    display_name = _clean_display_name(stem) or _clean_display_name(raw_stem) or "未命名模型"
    key = re.sub(r"[^\w]+", " ", display_name.casefold(), flags=re.UNICODE)
    key = re.sub(r"\s+", " ", key).strip()
    variant = " · ".join(tags) if tags else "标准版"
    return key or display_name.casefold(), display_name, variant, tags


def annotate_model_groups(rows: list[dict]) -> None:
    """Add stable model-group metadata to archive catalog rows in place."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        key, name, variant, tags = model_group_info(str(row.get("filename", "")))
        row["model_group_key"] = key
        row["model_group_name"] = name
        row["model_variant"] = variant
        row["model_variant_tags"] = tags
        groups.setdefault(key, []).append(row)

    for members in groups.values():
        grouped = len(members) > 1
        for row in members:
            row["model_group"] = grouped
            row["model_group_total"] = len(members) if grouped else 1
