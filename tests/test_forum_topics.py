from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace

import pytest
from telethon.tl.functions.messages import GetForumTopicsRequest
from telethon.tl.types import ForumTopic, MessageMediaPhoto

from test_workbench_actions import app
from test_server_sync import service, desktop, settled
from xianyu_pipeline import adapters, web_ui
from xianyu_pipeline.adapters import TelethonTelegramAdapter
from xianyu_pipeline.browser_download import DownloadCancelled
from xianyu_pipeline.channel_catalog import ChannelCatalog
from xianyu_pipeline.channel_sources import ChannelDirectory
from xianyu_pipeline.config import PipelineConfig
from xianyu_pipeline.data_protocol import validate_record
from xianyu_pipeline.telegram_sources import source_scope, split_scope, message_topic_id
from xianyu_pipeline.workbench import TransferStore


def topic(identity, title="Models", **fields):
    return {"topic_id": identity, "title": title, **fields}


def forum(directory, key="forum_models"):
    directory.ensure(key)
    directory.merge_topics(key, [topic(1, "General"), topic(40, "Flexi"),
                                  topic(70, "Movies", closed=True, hidden=True)])
    return directory.get(key)


def message(mid, topic_id=40, *, photo=False, name=None, **fields):
    reply = SimpleNamespace(forum_topic=True, reply_to_top_id=topic_id, reply_to_msg_id=topic_id)
    return SimpleNamespace(id=mid, post=False, reply_to=reply, message="fixture",
                           fwd_from=None, date=None, document=None,
                           file=SimpleNamespace(name=name, size=100) if name else None,
                           media=MessageMediaPhoto() if photo else None,
                           photo=object() if photo else None, grouped_id=900 if photo else None,
                           **fields)


def telegram(tmp_path, monkeypatch, history=(), *, topic_id=40, stop=None):
    adapter = TelethonTelegramAdapter(123, "a" * 32, tmp_path / "session", tmp_path / "inbox",
                                     source_kind="forum", topic_id=topic_id, stop_event=stop)

    class Client:
        disconnected = False
        requests = []

        def connect(self):
            pass

        def disconnect(self):
            self.disconnected = True

        def is_user_authorized(self):
            return True

        def get_entity(self, target):
            return SimpleNamespace(forum=True, megagroup=True)

        def get_input_entity(self, target):
            return target

        def iter_messages(self, target, **options):
            self.requests.append(options)
            rows = [row for row in history if row.id < (options.get("offset_id") or 2**31)]
            if options.get("reply_to"):
                rows = [row for row in rows if message_topic_id(row) == options["reply_to"]]
            return iter(rows[:options["limit"]])

        def get_messages(self, target, *, ids):
            return next((row for row in history if row.id == ids), None)

    client = Client()
    monkeypatch.setattr(adapter, "_client", lambda: client)
    return adapter, client


def test_scope_identity_and_general_classification():
    assert source_scope("https://t.me/Models", 40) == "models~40"
    assert split_scope("-100123~70") == ("-100123", 70)
    for key in ("../models~40", "models~0", "models~a", "models~01", "models~2147483648"):
        with pytest.raises(ValueError):
            split_scope(key)
    assert message_topic_id(message(90, 70)) == 70
    assert message_topic_id(SimpleNamespace(reply_to=None)) == 1
    reply = SimpleNamespace(forum_topic=False, reply_to_msg_id=38)
    assert message_topic_id(SimpleNamespace(reply_to=reply)) == 1
    reply.forum_topic = True
    reply.reply_to_top_id = None
    assert message_topic_id(SimpleNamespace(reply_to=reply)) == 38


def test_directory_topics_persist_and_do_not_get_lost_on_account_refresh(tmp_path):
    directory = ChannelDirectory(tmp_path)
    forum(directory)
    directory.rename("forum_models", "Custom forum")
    directory.merge_joined([{"key": "forum_models", "target": "forum_models", "title": "API title",
                              "kind": "forum", "forum": True}])
    loaded = ChannelDirectory(tmp_path)
    assert loaded.get("forum_models")["name"] == "Custom forum"
    assert loaded.get("forum_models")["topics_scanned_at"]
    assert [row["topic_id"] for row in loaded.topics("forum_models")] == [1, 40, 70]
    assert loaded.get_topic("forum_models", 70)["closed"]
    before = directory.path.read_bytes()
    with pytest.raises(ValueError):
        directory.merge_topics("forum_models", [topic(0)])
    assert directory.path.read_bytes() == before
    assert len(directory.topics("forum_models")) == 3
    directory.remove("forum_models")
    assert not directory.data["topics"]


def test_topic_scan_paginates_and_uses_top_message_date(tmp_path, monkeypatch):
    adapter, client = telegram(tmp_path, monkeypatch)
    date = datetime(2026, 10, 22, tzinfo=timezone.utc)

    def api_topic(identity):
        row = ForumTopic.__new__(ForumTopic)
        row.id, row.title, row.top_message = identity, f"Topic {identity}", identity + 100
        row.closed, row.hidden, row.date = False, identity == 1, date
        return row

    requests = []

    async def call(request):
        assert isinstance(request, GetForumTopicsRequest)
        requests.append(request)
        identity = 40 if len(requests) == 1 else 1
        return SimpleNamespace(count=2, topics=[api_topic(identity)], order_by_create_date=False,
                               messages=[SimpleNamespace(id=identity + 100, date=date)])

    monkeypatch.setattr(type(client), "__call__", lambda self, request: call(request), raising=False)
    rows = adapter.list_forum_topics("-100123")
    assert [row["topic_id"] for row in rows] == [40, 1]
    assert rows[1]["hidden"]
    assert requests[0].offset_topic == 0
    assert requests[1].offset_topic == 40 and requests[1].offset_id == 140
    assert requests[1].offset_date == date and client.disconnected


def test_topic_scan_cancellation_disconnects_and_preserves_directory(tmp_path, monkeypatch):
    stop = Event()
    adapter, client = telegram(tmp_path, monkeypatch, stop=stop)
    directory = ChannelDirectory(tmp_path)
    forum(directory)
    before = directory.path.read_bytes()

    async def call(request):
        stop.set()
        await asyncio.sleep(10)

    monkeypatch.setattr(type(client), "__call__", lambda self, request: call(request), raising=False)
    with pytest.raises(DownloadCancelled):
        adapter.list_forum_topics("forum_models")
    assert client.disconnected and directory.path.read_bytes() == before


def test_topic_scan_refuses_nonforum_and_no_authorization(tmp_path, monkeypatch):
    adapter, client = telegram(tmp_path, monkeypatch)
    monkeypatch.setattr(client, "is_user_authorized", lambda: False)
    with pytest.raises(RuntimeError, match="尚未登录"):
        adapter.list_forum_topics("models")
    monkeypatch.setattr(client, "is_user_authorized", lambda: True)
    monkeypatch.setattr(client, "get_entity", lambda target: SimpleNamespace(forum=False))
    with pytest.raises(ValueError, match="未开启"):
        adapter.list_forum_topics("models")


@pytest.mark.parametrize("selected,expected", [(40, [100]), (70, [99]), (1, [98])])
def test_archive_listing_stays_within_topic_and_allows_member_posts(tmp_path, monkeypatch, selected, expected):
    history = [message(100, 40, name="flexi.zip"), message(99, 70, name="movie.rar"),
               message(98, 1, name="general.zip")]
    adapter, client = telegram(tmp_path, monkeypatch, history, topic_id=selected)
    result = adapter.list_archives("forum_models", limit=2, preview_dir=tmp_path / "previews",
                                   known_covers={mid: {"cover_path": "fixture.jpg"} for mid in expected})
    assert [row["message_id"] for row in result["messages"]] == expected
    assert result["messages"][0]["topic_id"] == selected
    assert result["messages"][0]["chat"] == "forum_models"
    assert (client.requests[0].get("reply_to") == selected) is (selected != 1)


def test_direct_download_id_cannot_escape_selected_topic(tmp_path, monkeypatch):
    adapter, client = telegram(tmp_path, monkeypatch, [message(99, 70, name="other.zip")])
    with pytest.raises(ValueError, match="当前来源话题"):
        adapter.fetch_messages("forum_models", [99])
    assert client.disconnected and not list(adapter.download_dir.glob("*.zip"))


def test_successful_download_and_transfer_store_keep_original_topic(tmp_path, monkeypatch):
    adapter, _ = telegram(tmp_path, monkeypatch, [message(100, name="model.zip")])
    config = PipelineConfig(inbox_dir=adapter.download_dir, output_dir=tmp_path / "output",
                            work_dir=tmp_path / "work")
    store = TransferStore(config)
    adapter.on_event = store.event

    async def download(client, item, filename, transfer, scheduler):
        import zipfile
        path = adapter.download_dir / filename
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("model.stl", "fixture model")
        transfer.emit("completed", local_path=str(path), received=path.stat().st_size)
        return path

    monkeypatch.setattr(adapter, "_download_archive_async", download)
    messages = adapter.fetch_messages("forum_models", [100])
    assert messages[0].topic_id == 40 and messages[0].source_kind == "forum"
    store.accept_message(messages[0])
    task = store.snapshot()["tasks"][0]
    assert task["topic_id"] == 40 and task["source_kind"] == "forum"
    loaded = TransferStore(config).get(task["id"])
    assert loaded["topic_id"] == 40 and loaded["chat"] == "forum_models"


def test_forum_requires_topic_even_if_saved_as_old_channel(tmp_path, monkeypatch):
    adapter, _ = telegram(tmp_path, monkeypatch, topic_id=0)
    adapter.source_kind = "channel"
    with pytest.raises(ValueError, match="选择话题"):
        adapter.list_archives("forum_models", limit=1, preview_dir=tmp_path / "previews")


def test_regular_supergroup_member_posts_supported_broadcast_filter_unchanged(tmp_path, monkeypatch):
    adapter, _ = telegram(tmp_path, monkeypatch, topic_id=0)
    adapter.source_kind = "supergroup"
    assert adapter._is_channel_post(message(99, name="model.zip"))
    adapter.source_kind = "channel"
    assert not adapter._is_channel_post(message(99, name="model.zip"))


def test_photo_association_skips_other_topics_but_keeps_same_topic_boundaries(tmp_path, monkeypatch):
    history = [message(99, photo=True), message(98, 70, name="other.zip"),
               message(97, photo=True), message(96), message(95, photo=True)]
    adapter, client = telegram(tmp_path, monkeypatch, history)
    assert [row.id for row in adapter._preceding_photo_group(history, 100)] == [97, 99]
    scoped = asyncio.run(adapter._photo_history(client, "forum_models", 100))
    assert [row.id for row in scoped] == [99, 97, 96, 95]
    adapter.topic_id = 1
    history[:] = [message(99, 70, photo=True), message(98, 1, photo=True),
                  message(97, 1), message(96, 1, photo=True)]
    scoped = asyncio.run(adapter._photo_history(client, "forum_models", 100))
    assert [row.id for row in adapter._preceding_photo_group(scoped, 100)] == [98]
    assert "reply_to" not in client.requests[-1]


def test_topic_catalogs_and_cursors_are_independent_and_survive_restart(tmp_path):
    catalog = ChannelCatalog(tmp_path)
    for identity, mid in ((40, 100), (70, 90)):
        catalog.merge(source_scope("forum_models", identity), {
            "messages": [{"message_id": mid, "chat": "forum_models", "topic_id": identity, "filename": "model.zip"}],
            "oldest_scanned_id": mid - 6, "has_more": True})
    loaded = ChannelCatalog(tmp_path)
    assert loaded.snapshot("forum_models~40")["oldest_scanned_id"] == 94
    assert loaded.snapshot("forum_models~70")["oldest_scanned_id"] == 84
    assert not loaded.snapshot("forum_models")["messages"]
    with pytest.raises(ValueError, match="不属于"):
        loaded.merge("forum_models~40", {"messages": [{"message_id": 90, "chat": "forum_models", "topic_id": 70}],
                                         "oldest_scanned_id": 80, "has_more": False})


def test_backend_select_validates_topics_and_requires_explicit_choice(app):
    row = forum(app.channels)
    assert app.post("/api/channels/select", {"key": row["key"]})[0] == 200
    assert app.post("/api/channel-scan")[0] == 400
    assert app.post("/api/channels/select", {"key": row["key"], "topic_id": 999})[0] == 400
    assert app.post("/api/channels/select", {"key": "fixture_channel", "topic_id": 40})[0] == 400
    assert app.post("/api/channels/select", {"key": row["key"], "topic_id": 40})[0] == 200
    assert PipelineConfig.from_json(app.config_path).source_topic_id == 40
    assert app.get("/api/channels")[1]["active_topic_id"] == 40
    assert app.get("/api/channel-messages")[1]["channel"] == "forum_models~40"
    assert app.post("/api/channels/select", {"key": "fixture_channel"})[0] == 200
    assert PipelineConfig.from_json(app.config_path).source_topic_id == 0
    app.stop()


def test_backend_topic_scan_saves_and_exposes_errors_without_erasing(app, monkeypatch):
    row = forum(app.channels)

    def scan(self, channel):
        assert channel == row["url"]
        return [topic(1, "General"), topic(45, "New files")]

    monkeypatch.setattr(web_ui.TelethonTelegramAdapter, "list_forum_topics", scan)
    assert app.post("/api/channel-topics-scan", {"key": row["key"]})[0] == 200
    assert app.post("/api/channels/rename", {"key": row["key"], "name": "changed"})[0] == 409
    app.run()
    assert [item["topic_id"] for item in app.channels.topics(row["key"])] == [1, 45]
    assert app.get("/api/workbench")[1]["telegram_topics"]["stage"] == "completed"
    before = app.channels.path.read_bytes()

    def failure(*args):
        raise RuntimeError("fixture network error")

    monkeypatch.setattr(web_ui.TelethonTelegramAdapter, "list_forum_topics", failure)
    assert app.post("/api/channel-topics-scan", {"key": row["key"]})[0] == 200
    app.run()
    assert app.channels.path.read_bytes() == before
    assert app.get("/api/workbench")[1]["telegram_topics"]["stage"] == "failed"


def test_retry_uses_original_topic_even_after_switching_to_broadcast(app, monkeypatch):
    identity = app.store.event({"chat": "forum_models", "message_id": 99, "filename": "model.zip",
                                "topic_id": 70, "source_kind": "forum", "download_status": "failed"})
    calls = []

    def fetch(self, chat, ids, channel, mode, limit):
        calls.append((self.topic_id, self.source_kind, chat, ids))
        return []

    monkeypatch.setattr(web_ui.TelethonTelegramAdapter, "fetch_messages", fetch)
    assert app.post("/api/retry", {"id": identity})[0] == 200
    app.run()
    assert calls == [(70, "forum", "forum_models", [99])]


def test_topics_and_scoped_cursors_sync_between_devices(tmp_path, service):
    first, second = desktop(tmp_path / "first", service), desktop(tmp_path / "second", service)
    forum(first.channels)
    for topic_id, mid in ((40, 100), (70, 90)):
        first.catalog.merge(source_scope("forum_models", topic_id), {
            "messages": [{"message_id": mid, "chat": "forum_models", "topic_id": topic_id,
                           "source_kind": "forum", "filename": "model.rar"}],
            "oldest_scanned_id": mid - 5, "has_more": True})
    settled(first)
    settled(second)
    assert [row["topic_id"] for row in second.channels.topics("forum_models")] == [1, 40, 70]
    assert second.catalog.snapshot("forum_models~70")["oldest_scanned_id"] == 85
    assert second.catalog.snapshot("forum_models~40")["messages"][0]["chat"] == "forum_models"
    first.channels.merge_topics("forum_models", [topic(1, "General"), topic(70, "Updated")])
    settled(first)
    settled(second)
    assert [row["topic_id"] for row in second.channels.topics("forum_models")] == [1, 70]
    assert second.channels.get_topic("forum_models", 70)["title"] == "Updated"


def test_protocol_rejects_mismatched_topic_message():
    validate_record("messages", "models~40:100", {"chat": "models", "topic_id": 40, "message_id": 100})
    with pytest.raises(ValueError):
        validate_record("messages", "models~40:100", {"chat": "models", "topic_id": 70, "message_id": 100})
    with pytest.raises(ValueError):
        validate_record("topics", "models~40", {"key": "models~40", "channel": "other", "topic_id": 40, "title": "Title"})
