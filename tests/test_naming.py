from __future__ import annotations

from threading import Event

import httpx
import pytest

from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.naming import NameTranslator, glossary, validate_name
from xianyu_pipeline.browser_download import DownloadCancelled


def translator(tmp_path, **settings):
    return NameTranslator(PipelineConfig(output_dir=tmp_path, rename_translation_provider="local", **settings))


def test_local_terms_keep_format_version_and_parts(tmp_path):
    result = translator(tmp_path).suggest("Dragon Warrior Bust v2.part01.rar")
    assert result["chinese_filename"] == "龙战士 胸像_v2_part01.rar"
    assert result["naming_status"] == "review"
    assert result["naming_source"] == "local"


def test_custom_terms_override_builtins_and_longest_match_wins(tmp_path):
    result = translator(tmp_path, rename_glossary="Dragon Warrior=神龙战士\nBust=半身像").suggest("Dragon Warrior Bust.zip")
    assert result["chinese_filename"] == "神龙战士 半身像.zip"


def test_unknown_names_are_preserved_instead_of_invented(tmp_path):
    result = translator(tmp_path).suggest("XYZStudio Qwerty Figure STL 1_6 v1.2.zip")
    assert result["chinese_filename"] == "XYZStudio Qwerty 手办_STL_1_6_v1.2.zip"
    assert result["naming_quality"] == "partial"
    assert result["naming_untranslated"] == ["XYZStudio", "Qwerty"]
    assert "STL" not in result["naming_note"]


@pytest.mark.parametrize("work,character", [
    ("Steins Gate", "Kurisu Makise"), ("Steins;Gate", "Makise Kurisu"),
    ("STEINS-GATE", "KURISU_MAKISE"), ("SteinsGate", "Makise-Kurisu"),
    ("Steins_Gate", "Kurisu.Makise"),
])
def test_verified_work_and_character_aliases_preserve_unknown_maker(tmp_path, monkeypatch, work, character):
    cloud = NameTranslator(PipelineConfig(output_dir=tmp_path))
    monkeypatch.setattr(cloud, "_online", lambda query: pytest.fail("Do not guess a maker's name"))
    result = cloud.suggest(f"Kuru Figure - {work} - {character}.rar")
    assert result["chinese_filename"] == "Kuru 手办 - 命运石之门 - 牧濑红莉栖.rar"
    assert result["naming_source"] == "local"
    assert result["naming_quality"] == "partial"
    assert result["naming_untranslated"] == ["Kuru"]
    assert "未识别：Kuru" in result["naming_note"]
    assert "作品/角色：命运石之门、牧濑红莉栖" in result["naming_note"]


def test_custom_aliases_override_published_names(tmp_path):
    result = translator(tmp_path, rename_glossary="Steins;Gate=命运石之门系列\nMakise Kurisu=红莉栖").suggest(
        "Steins;Gate - Makise_Kurisu Figure.rar")
    assert result["chinese_filename"] == "命运石之门系列 - 红莉栖 手办.rar"
    assert result["naming_quality"] == "matched"


def test_name_aliases_do_not_match_inside_other_names(tmp_path):
    result = translator(tmp_path).suggest("NotSteinsGate - Kurisu Makise123 Figure.rar")
    assert result["chinese_filename"] == "NotSteinsGate - Kurisu Makise123 手办.rar"
    assert result["naming_quality"] == "partial"


def test_custom_brand_kept_in_latin_is_still_a_known_term(tmp_path):
    result = translator(tmp_path, rename_glossary="Kuru=Kuru工作室").suggest(
        "Kuru Figure - Steins Gate - Kurisu Makise.rar")
    assert result["chinese_filename"] == "Kuru工作室 手办 - 命运石之门 - 牧濑红莉栖.rar"
    assert result["naming_untranslated"] == []
    assert result["naming_quality"] == "matched"


@pytest.mark.parametrize("candidate", ["../模型.rar", "模型.zip", "CON.rar", "模:型.rar", "", 123, "模型" * 81 + ".rar"])
def test_invalid_names_rejected(candidate):
    with pytest.raises(ValueError):
        validate_name(candidate, "original.rar")


@pytest.mark.parametrize("candidate", ["模型_v2.rar", "模型_part2.rar", "模型.rar"])
def test_versions_parts_and_formats_cannot_be_removed(candidate):
    with pytest.raises(ValueError, match="保留"):
        validate_name(candidate, "Figure v2 part2 STL.rar")


def test_confirmed_memory_survives_restart(tmp_path):
    first = translator(tmp_path)
    first.remember("Special Figure.rar", "特殊手办.rar")
    result = translator(tmp_path).suggest("Special Figure.rar")
    assert result["chinese_filename"] == "特殊手办.rar"
    assert result["naming_source"] == "memory"
    assert result["naming_quality"] == "confirmed"


def test_invalid_glossary_is_rejected():
    with pytest.raises(ValueError):
        glossary("not a mapping")


@pytest.mark.parametrize("key", ["---", "___", "...", "_ - ."])
def test_separator_only_terms_cannot_create_empty_pattern_matches(key):
    with pytest.raises(ValueError, match="分隔符"):
        glossary(f"{key}=模型")


@pytest.mark.parametrize("failure", [False, True, "quota", "fuzzy"])
def test_online_translation_uses_whole_match_or_clear_local_fallback(tmp_path, monkeypatch, failure):
    original_client = httpx.Client
    calls = []

    def handle(request):
        calls.append(request)
        assert "cookie" not in request.headers and "authorization" not in request.headers
        assert request.url.params["q"] == "UnknownObject"
        if failure is True:
            raise httpx.ReadTimeout("fixture", request=request)
        return httpx.Response(200, json={
            "responseStatus": 200, "quotaFinished": failure == "quota",
            "responseData": {"translatedText": "错误模糊译名"},
            "matches": [{"segment": "other" if failure == "fuzzy" else "UnknownObject", "translation": "未知物体"}],
        })

    monkeypatch.setattr("xianyu_pipeline.naming.httpx.Client",
                        lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))
    cloud = NameTranslator(PipelineConfig(output_dir=tmp_path, rename_translation_provider="mymemory"))
    result = cloud.suggest("UnknownObject v2.zip")
    assert len(calls) == 1
    assert result["naming_status"] == "review"
    if failure:
        assert result["chinese_filename"] == "UnknownObject_v2.zip"
        assert result["naming_source"] == "local"
        assert result["naming_note"]
    else:
        assert result["chinese_filename"] == "未知物体_v2.zip"
        assert result["naming_quality"] == "machine"


@pytest.mark.parametrize("reply", ["Mystery Castle 手办", "手办", "Mystery Castle"])
def test_echo_or_dropped_untranslated_words_is_not_online_success(tmp_path, monkeypatch, reply):
    original_client = httpx.Client

    def handle(request):
        assert request.url.params["q"] == "Mystery Castle"
        return httpx.Response(200, json={
            "responseStatus": 200,
            "matches": [{"segment": "Mystery Castle", "translation": reply}],
        })

    monkeypatch.setattr("xianyu_pipeline.naming.httpx.Client",
                        lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))
    result = NameTranslator(PipelineConfig(output_dir=tmp_path)).suggest("Mystery Castle Figure.rar")
    assert result["chinese_filename"] == "Mystery Castle 手办.rar"
    assert result["naming_source"] == "local"
    assert result["naming_quality"] == "partial"
    assert result["naming_untranslated"] == ["Mystery", "Castle"]
    assert "在线译文" not in result["naming_note"]


def test_online_uses_original_language_and_preserves_custom_terms(tmp_path, monkeypatch):
    cloud = NameTranslator(PipelineConfig(output_dir=tmp_path, rename_glossary="Figure=人偶"))

    def translate(query):
        assert query == "Mystery Castle"
        return "神秘城堡"

    monkeypatch.setattr(cloud, "_online", translate)
    result = cloud.suggest("Mystery_Castle_Figure_STL_v2.zip")
    assert result["chinese_filename"] == "神秘城堡 人偶_STL_v2.zip"
    assert result["naming_quality"] == "machine"
    assert result["naming_untranslated"] == []


def test_online_can_skip_a_mixed_language_echo_for_a_real_exact_translation(tmp_path, monkeypatch):
    original_client = httpx.Client

    def handle(request):
        query = request.url.params["q"]
        assert query == "Mystery Castle"
        return httpx.Response(200, json={
            "responseStatus": 200, "matches": [
                {"segment": query, "translation": "Mystery Castle 手办"},
                {"segment": query, "translation": "神秘城堡"},
            ],
        })

    monkeypatch.setattr("xianyu_pipeline.naming.httpx.Client",
                        lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))
    result = NameTranslator(PipelineConfig(output_dir=tmp_path)).suggest("Mystery Castle Figure.rar")
    assert result["chinese_filename"] == "神秘城堡 手办.rar"
    assert result["naming_source"] == "mymemory"
    assert result["naming_quality"] == "machine"


def test_partial_machine_translation_identifies_remaining_unknowns(tmp_path, monkeypatch):
    cloud = NameTranslator(PipelineConfig(output_dir=tmp_path))
    monkeypatch.setattr(cloud, "_online", lambda query: "古代 XYZ")
    result = cloud.suggest("Ancient XYZ Figure.rar")
    assert result["chinese_filename"] == "古代 XYZ 手办.rar"
    assert result["naming_source"] == "mymemory"
    assert result["naming_quality"] == "partial"
    assert result["naming_untranslated"] == ["XYZ"]


def test_online_does_not_accept_an_echo_of_an_already_chinese_query(tmp_path, monkeypatch):
    original_client = httpx.Client

    def handle(request):
        query = request.url.params["q"]
        return httpx.Response(200, json={
            "responseStatus": 200, "matches": [{"segment": query, "translation": query}],
        })

    monkeypatch.setattr("xianyu_pipeline.naming.httpx.Client",
                        lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))
    result = NameTranslator(PipelineConfig(output_dir=tmp_path)).suggest("原作 Unknown Figure.rar")
    assert result["naming_source"] == "local"
    assert result["naming_quality"] == "partial"


def test_online_never_receives_confirmed_terms(tmp_path, monkeypatch):
    cloud = NameTranslator(PipelineConfig(output_dir=tmp_path, rename_translation_provider="mymemory",
                                         rename_glossary="Qwerty=固定译名"))
    def translate(query):
        assert query == "unknown"
        return "未知"

    monkeypatch.setattr(cloud, "_online", translate)
    result = cloud.suggest("Qwerty unknown.zip")
    assert result["chinese_filename"] == "固定译名 未知.zip"
    assert result["naming_source"] == "mymemory"


def test_descriptions_are_translated_without_sending_work_or_character_names(tmp_path, monkeypatch):
    cloud = NameTranslator(PipelineConfig(output_dir=tmp_path))

    def translate(query):
        assert query == "Winter Outfit"
        return "冬装"

    monkeypatch.setattr(cloud, "_online", translate)
    result = cloud.suggest("Steins Gate - Kurisu Makise - Winter Outfit Figure.rar")
    assert result["chinese_filename"] == "命运石之门 - 牧濑红莉栖 - 冬装 手办.rar"
    assert result["naming_quality"] == "machine"


def test_one_failed_fragment_does_not_erase_another_successful_translation(tmp_path, monkeypatch):
    cloud = NameTranslator(PipelineConfig(output_dir=tmp_path))

    def translate(query):
        if query == "Winter Outfit":
            return "冬装"
        assert query == "XYZBrand"
        raise ValueError("译名不可用")

    monkeypatch.setattr(cloud, "_online", translate)
    result = cloud.suggest("XYZBrand Figure - Winter Outfit.rar")
    assert result["chinese_filename"] == "XYZBrand 手办 - 冬装.rar"
    assert result["naming_quality"] == "partial"
    assert result["naming_untranslated"] == ["XYZBrand"]
    assert "译名不可用" in result["naming_note"]


def test_translation_can_stop_between_fragments(tmp_path, monkeypatch):
    stop = Event()
    cloud = NameTranslator(PipelineConfig(output_dir=tmp_path), stop)
    def translate(query):
        assert query == "Winter Outfit"
        stop.set()
        return "冬装"

    monkeypatch.setattr(cloud, "_online", translate)
    with pytest.raises(DownloadCancelled):
        cloud.suggest("Winter Outfit Figure XYZBrand.rar")


def test_exhausted_quota_does_not_repeat_network_wait_for_every_file(tmp_path, monkeypatch):
    original_client = httpx.Client
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"responseStatus": 403, "quotaFinished": True})

    monkeypatch.setattr("xianyu_pipeline.naming.httpx.Client",
                        lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw))
    cloud = NameTranslator(PipelineConfig(output_dir=tmp_path))
    first, second = cloud.suggest("UnknownOne.zip"), cloud.suggest("UnknownTwo.zip")
    assert first["naming_source"] == second["naming_source"] == "local"
    assert first["naming_note"].split("；未识别")[0] == second["naming_note"].split("；未识别")[0]
    assert first["naming_untranslated"] == ["UnknownOne"]
    assert second["naming_untranslated"] == ["UnknownTwo"]
    assert len(calls) == 1


def test_stop_before_translation_does_not_start_network(tmp_path):
    stop = Event()
    stop.set()
    cloud = NameTranslator(PipelineConfig(output_dir=tmp_path), stop)
    with pytest.raises(DownloadCancelled):
        cloud.suggest("file.rar")
