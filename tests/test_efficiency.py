"""Regression tests for the efficiency work.

These cover the parts that changed shape rather than behaviour: the pooled
Bot API transport in cover.py and the batched/paged archive. Nothing here
touches the network or requires Pyrogram.

Run with:  python -m pytest tests -q
"""

import json
import os
import tempfile

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "hash")
os.environ.setdefault("BOT_TOKEN", "TESTTOKEN")
os.environ.setdefault(
    "DB_PATH",
    os.path.join(tempfile.gettempdir(), "caption_bot_tests.sqlite3"),
)

from caption_bot import archive, cover  # noqa: E402


# ---------------------------------------------------------------------------
# cover.py transport
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    def read(self):
        return self._payload


class FakeConnection:
    """Stands in for http.client.HTTPSConnection."""

    def __init__(self, script=None):
        self.requests = []
        self.closed = 0
        self.script = list(script or [])

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))

    def getresponse(self):
        if self.script:
            action = self.script.pop(0)

            if isinstance(action, Exception):
                raise action

            return action

        return FakeResponse(
            200,
            json.dumps({"ok": True, "result": {"message_id": 1}}).encode(),
        )

    def close(self):
        self.closed += 1


def _install(conn):
    cover._connection = conn
    return conn


def _send(caption="S01E01.Show.mkv", **extra):
    return cover.send_video_with_cover(
        chat_id=-100,
        video_file_id="vid",
        caption=caption,
        cover_file_id="cov",
        **extra,
    )


def test_repeated_sends_reuse_one_connection():
    conn = _install(FakeConnection())

    for _ in range(5):
        _send()

    assert len(conn.requests) == 5
    assert conn.closed == 0


def test_request_targets_send_video_with_the_bot_token():
    conn = _install(FakeConnection())
    _send()

    method, path, body, headers = conn.requests[0]

    assert method == "POST"
    assert path == "/botTESTTOKEN/sendVideo"
    assert headers["Connection"] == "keep-alive"
    assert b"video=vid" in body
    assert b"cover=cov" in body


def test_optional_fields_are_omitted_when_unset():
    conn = _install(FakeConnection())
    _send(width=None, height=0, duration=60)

    body = conn.requests[0][2]

    assert b"duration=60" in body
    assert b"width=" not in body
    assert b"height=" not in body


def test_a_dropped_keepalive_socket_is_rebuilt_and_retried():
    conn = _install(
        FakeConnection(
            script=[
                ConnectionResetError("stale socket"),
                FakeResponse(
                    200,
                    json.dumps(
                        {"ok": True, "result": {"message_id": 7}}
                    ).encode(),
                ),
            ]
        )
    )

    # The retry path replaces the connection, so keep a handle on the
    # original and let the factory hand the same fake back.
    original_factory = cover._get_connection
    cover._get_connection = lambda: conn

    try:
        result = _send()
    finally:
        cover._get_connection = original_factory

    assert result["message_id"] == 7
    assert conn.closed == 1


def test_telegram_error_becomes_cover_send_error():
    _install(
        FakeConnection(
            script=[
                FakeResponse(
                    200,
                    json.dumps(
                        {"ok": False, "description": "VIDEO_COVER_INVALID"}
                    ).encode(),
                )
            ]
        )
    )

    try:
        _send()
    except cover.CoverSendError as error:
        assert "VIDEO_COVER_INVALID" in str(error)
    else:
        raise AssertionError("expected CoverSendError")


def test_http_error_and_bad_json_become_cover_send_error():
    _install(FakeConnection(script=[FakeResponse(500, b"boom")]))

    try:
        _send()
    except cover.CoverSendError as error:
        assert "HTTP 500" in str(error)
    else:
        raise AssertionError("expected CoverSendError")

    _install(FakeConnection(script=[FakeResponse(200, b"not json")]))

    try:
        _send()
    except cover.CoverSendError as error:
        assert "invalid JSON" in str(error)
    else:
        raise AssertionError("expected CoverSendError")


# ---------------------------------------------------------------------------
# archive.py
# ---------------------------------------------------------------------------


OWNER = 10_000_001


def _rows(season, count):
    return [
        {
            "owner_id": OWNER,
            "season": season,
            "episode": episode,
            "caption": f"S{season:02d}E{episode:02d}.Show.mkv",
            "media_type": "video",
            "file_id": f"file-{season}-{episode}",
            "width": 1920,
            "height": 1080,
            "duration": 60,
            "supports_streaming": True,
            "has_spoiler": False,
            "cover_file_id": "cov" if episode % 2 else None,
        }
        for episode in range(1, count + 1)
    ]


def test_batch_write_then_read_back():
    assert archive.record_episodes(_rows(1, 12)) == 12
    assert archive.record_episodes([]) == 0

    episodes = archive.get_season_episodes(OWNER, 1)

    assert [e.episode for e in episodes] == list(range(1, 13))
    assert episodes[0].caption == "S01E01.Show.mkv"
    assert episodes[0].supports_streaming is True
    assert episodes[0].cover_file_id == "cov"
    assert episodes[1].cover_file_id is None


def test_batch_write_upserts_on_conflict():
    rows = _rows(1, 1)
    rows[0]["caption"] = "REPROCESSED"
    archive.record_episodes(rows)

    assert archive.get_season_episodes(OWNER, 1)[0].caption == "REPROCESSED"
    assert archive.season_episode_count(OWNER, 1) == 12


def test_season_counts_matches_list_seasons_and_per_season_counts():
    archive.record_episodes(_rows(2, 3))

    counts = archive.season_counts(OWNER)

    assert [season for season, _ in counts] == archive.list_seasons(OWNER)
    assert dict(counts)[1] == archive.season_episode_count(OWNER, 1)
    assert dict(counts)[2] == 3
    assert archive.has_any_season(OWNER) is True
    assert archive.has_any_season(OWNER + 1) is False


def test_paged_reads_equal_a_full_read():
    full = archive.get_season_episodes(OWNER, 1)

    paged = []
    after = 0

    while True:
        page = archive.fetch_season_page(OWNER, 1, after, limit=5)

        if not page:
            break

        after = page[-1].episode
        paged.extend(page)

    assert [(e.episode, e.file_id) for e in paged] == [
        (e.episode, e.file_id) for e in full
    ]


def test_transfer_tokens_round_trip():
    token = archive.create_transfer(OWNER, [1, 2])
    record = archive.get_transfer(token)

    assert record.owner_id == OWNER
    assert record.seasons == [1, 2]
    assert archive.get_transfer("missing-token") is None
