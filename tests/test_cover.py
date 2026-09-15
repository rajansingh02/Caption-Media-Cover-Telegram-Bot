from caption_bot.models import BatchItem, Episode, UserState


def test_video_batch_item_stores_server_side_file_id_and_metadata():
    item = BatchItem(
        message_id=10,
        chat_id=20,
        filename="Show.S02.E01.mkv",
        display_name="Show.S02.E01.mkv",
        detected=Episode(2, 1),
        media_type="video",
        file_id="video-file-id",
        width=1920,
        height=1080,
        duration=3600,
        supports_streaming=True,
    )
    assert item.media_type == "video"
    assert item.file_id == "video-file-id"
    assert item.width == 1920
    assert item.height == 1080
    assert item.duration == 3600


def test_cover_is_user_state():
    state = UserState(cover_file_id="cover-file-id")
    assert state.cover_file_id == "cover-file-id"
