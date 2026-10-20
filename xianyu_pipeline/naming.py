"""Reviewable Chinese archive names; original filenames remain task identities."""
from __future__ import annotations

import html
import json
import logging
import re
import unicodedata
from pathlib import Path
from threading import Event

import httpx

from .browser_download import DownloadCancelled


TERMS = {
    "pre-supported": "预支撑", "pre supported": "预支撑", "unsupported": "无支撑",
    "for patrons": "会员版", "dragon warrior": "龙战士", "bust": "胸像",
    "miniature": "微缩模型", "figure": "手办", "figurine": "手办",
    "statue": "雕像", "dragon": "龙", "warrior": "战士", "helmet": "头盔",
    "head": "头部模型", "base": "底座", "terrain": "地形", "supported": "带支撑",
}
# Published names from Good Smile's product listings:
# https://www.goodsmilearts.com/Cn/Index/pageView/catid/5/id/353.html
# https://www.goodsmilearts.com/En/Index/pageView/catid/35/id/342.html
NAMES = {
    "steins gate": "命运石之门", "steins;gate": "命运石之门", "steinsgate": "命运石之门",
    "kurisu makise": "牧濑红莉栖", "makise kurisu": "牧濑红莉栖",
}
TOKENS = re.compile(r"(?i)(?<![a-z0-9])(?:part[._ -]?\d+|v\d+(?:\.\d+)*|1[:_-]\d+|stl|obj|fbx|3mf)(?![a-z0-9])")
CJK = re.compile(r"[\u3400-\u9fff]")
ENGLISH = re.compile(r"[a-zA-Z][a-zA-Z0-9]*(?:['’-][a-zA-Z0-9]+)*")


def _term_pattern(terms):
    # Accept common filename separators without matching names inside other words.
    alternatives = [r"[\s_.-]+".join(re.escape(part) for part in term.split())
                    for term in sorted(terms, key=len, reverse=True)]
    return re.compile(r"(?<![a-z0-9])(" + "|".join(alternatives) + r")(?![a-z0-9])", re.I)


def _term_key(value):
    return re.sub(r"[\s_.-]+", " ", value).strip().casefold()


def _untranslated(value, pinned=()):
    for term in pinned:
        value = re.sub(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", " ", value, flags=re.I)
    return list(dict.fromkeys(match.group() for match in ENGLISH.finditer(TOKENS.sub(" ", value))))


def _quality_note(candidate, note, source, pinned):
    unresolved = _untranslated(candidate, pinned)
    if unresolved:
        note += "；未识别：" + "、".join(unresolved) + "（保留原文，请核对或补充术语）"
    elif source == "mymemory":
        note += "；机器译名，含义仍需核对"
    return {"naming_note": note, "naming_untranslated": unresolved,
            "naming_quality": "partial" if unresolved else "machine" if source == "mymemory" else "matched"}


def validate_name(value, original):
    if not isinstance(value, str):
        raise ValueError("中文文件名必须为字符串")
    value = unicodedata.normalize("NFC", value.strip())
    if (not value or re.search(r'[\\/:*?"<>|\x00-\x1f]', value)
            or value.endswith((".", " ")) or len(value.encode("utf-8")) > 240
            or value in {".", ".."}):
        raise ValueError("文件名包含非法字符或过长（最多 240 字节）")
    if Path(original).suffix.lower() not in {".zip", ".rar"} or Path(value).suffix.lower() != Path(original).suffix.lower():
        raise ValueError("必须保留原 ZIP/RAR 扩展名")
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])", Path(value).stem):
        raise ValueError("文件名不能使用系统保留名称")
    expected = {token.casefold() for token in TOKENS.findall(Path(original).stem)}
    actual = {token.casefold() for token in TOKENS.findall(Path(value).stem)}
    if not expected <= actual:
        raise ValueError("请保留原文件名的版本、比例、格式和分卷编号")
    return value


def glossary(text):
    if not isinstance(text, str) or len(text) > 12000:
        raise ValueError("术语表必须为不超过 12000 字符的文本")
    result = {_term_key(key): value for key, value in {**TERMS, **NAMES}.items()}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("=", 1)
        if len(parts) != 2 or not all(part.strip() for part in parts):
            raise ValueError("术语表每行填写：英文名称=中文名称")
        key, value = (part.strip() for part in parts)
        if len(key) > 120 or len(value) > 120 or re.search(r'[\\/:*?"<>|\x00-\x1f]', value):
            raise ValueError("术语表名称过长或包含非法文件名字符")
        if not _term_key(key):
            raise ValueError("术语表名称不能仅包含空格或分隔符")
        result[_term_key(key)] = value
    return result


def validate_naming_settings(payload, existing):
    provider = payload.get("rename_translation_provider", existing.rename_translation_provider)
    if not isinstance(provider, str) or provider not in {"local", "mymemory"}:
        raise ValueError("翻译方式必须为本地术语或 MyMemory")
    glossary(payload.get("rename_glossary", existing.rename_glossary))


class NameTranslator:
    def __init__(self, config, stop_event: Event | None = None):
        self.provider = config.rename_translation_provider
        self.terms = glossary(config.rename_glossary)
        self.pattern = _term_pattern(self.terms)
        self.name_keys = {_term_key(key) for key in NAMES}
        self.stop_event = stop_event or Event()
        self.online_error = ""
        self.path = config.output_dir / "name-memory.json"
        self.memory = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("名称记忆格式无效，原文件已保留")
            self.memory = data

    def remember(self, original, candidate):
        self.memory[original.casefold()] = validate_name(candidate, original)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.memory, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def _online(self, query):
        if len(query.encode("utf-8")) > 500:
            raise ValueError("文件名超过在线翻译单次长度限制，已使用本地术语")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        try:
            with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
                response = client.get("https://api.mymemory.translated.net/get",
                                      params={"q": query, "langpair": "en|zh-CN"})
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError):
            self.online_error = "在线翻译连接失败，已使用本地术语，可编辑或稍后重新生成"
            raise ValueError(self.online_error) from None
        if not isinstance(data, dict) or data.get("quotaFinished") or data.get("responseStatus") != 200:
            self.online_error = "在线翻译不可用或配额已用完，已使用本地术语"
            raise ValueError(self.online_error)
        # Translation memory may prefer a fuzzy match that omits words; require the full segment.
        matches = data.get("matches", [])
        if not isinstance(matches, list):
            raise ValueError("在线译文格式无效，已使用本地术语")
        exact = [row for row in matches if isinstance(row, dict)
                 and str(row.get("segment", "")).casefold() == query.casefold()]
        # Ignore unchanged/mixed-language echoes, even when they already contain Chinese.
        texts = [html.unescape(row["translation"]) for row in exact if isinstance(row.get("translation"), str)]
        texts = [text for text in texts if CJK.search(text) and
                 [word.casefold() for word in _untranslated(text)] !=
                 [word.casefold() for word in _untranslated(query)]]
        if not texts:
            raise ValueError("未获得完整中文译文，已使用本地术语")
        return texts[0]

    def suggest(self, original):
        if self.stop_event.is_set():
            raise DownloadCancelled("中文命名已停止")
        remembered = self.memory.get(original.casefold())
        if remembered:
            return {"chinese_filename": validate_name(remembered, original), "naming_source": "memory",
                    "naming_note": "复用已确认名称", "naming_error": "", "naming_status": "review",
                    "naming_quality": "confirmed", "naming_untranslated": []}
        stem = Path(original).stem
        protected = TOKENS.findall(stem)
        content = TOKENS.sub(" ", stem)
        content = re.sub(r"[_]+", " ", content).strip(" .-_")
        # Captured matches occupy odd positions; only the original gaps go online.
        pieces = self.pattern.split(content)
        matched = pieces[1::2]
        pinned = [self.terms[_term_key(term)] for term in matched]
        names = list(dict.fromkeys(self.terms[_term_key(term)] for term in matched
                                   if _term_key(term) in self.name_keys))
        pieces[1::2] = pinned
        local = "".join(pieces)
        source, note = "local", "本地术语匹配"
        if names:
            note += "；作品/角色：" + "、".join(names)
        translated = local
        unresolved = _untranslated(local, pinned)
        # A lone unknown label next to identified characters may be a maker's name.
        # Preserve it rather than inventing a transliteration from insufficient context.
        preserve_label = bool(names) and len(unresolved) == 1
        if self.provider == "mymemory" and unresolved and not preserve_label:
            errors = []
            for index in range(0, len(pieces), 2):
                if self.stop_event.is_set():
                    raise DownloadCancelled("中文命名已停止")
                piece = pieces[index]
                span = re.search(r"[a-zA-Z](?:.*[a-zA-Z0-9])?", piece)
                if not span:
                    continue
                query = span.group()
                try:
                    if self.online_error:
                        raise ValueError(self.online_error)
                    result = self._online(query)
                    extra = result
                    for term in pinned:
                        extra = extra.replace(term, "")
                    if (not CJK.search(extra)
                            or [word.casefold() for word in _untranslated(result)] ==
                            [word.casefold() for word in _untranslated(query)]):
                        raise ValueError("在线结果未增加有效译文，已保留未识别原文")
                    pieces[index] = piece[:span.start()] + result + piece[span.end():]
                    source = "mymemory"
                except ValueError as exc:
                    errors.append(str(exc))
            translated = "".join(pieces)
            if source == "mymemory":
                note = "MyMemory 分段机器译名；已固定作品/角色及模型术语"
            if errors:
                note += "；" + errors[0]
        if self.stop_event.is_set():
            raise DownloadCancelled("中文命名已停止")
        translated = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", translated)
        translated = re.sub(r"\s+", " ", translated).strip(" .-_") or stem
        suffix = "_".join(protected)
        candidate = translated + ("_" + suffix if suffix else "") + Path(original).suffix
        candidate = validate_name(candidate, original)
        return {"chinese_filename": candidate, "naming_source": source,
                **_quality_note(translated, note, source, pinned),
                "naming_error": "", "naming_status": "review"}
