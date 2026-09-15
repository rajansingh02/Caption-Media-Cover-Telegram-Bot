# Caption Bot — Parts 1 + 2

Private Telegram bot for batching media, assigning smart SxxExx captions, copying media on Telegram's servers, deleting successful source messages after the complete copy phase, and applying Telegram's modern custom video-cover field.

## Caption features

- `/scaption S01E01.Name.mkv` starts a sequence.
- Smart episode detection accepts `S01E01`, `S01.E01`, `S01 E01`, `S01-E01`, and cases where Sxx and Exx are separated by other filename text.
- Filename season/episode markers are authoritative.
- If the active sequence is S01 and a new batch clearly contains S02 files, the new batch uses S02 automatically.
- Explicit batches are sorted by season and episode.
- Mixed batches use explicit filenames as anchors and infer missing episodes.
- `/preview` shows the final assignments.
- Manual up/down ordering is supported.
- Processing has two phases: copy/output everything first, then delete successful source messages.
- Failed source messages are never deleted.
- Batch completion has a bottom `🔄 Start Next Season` button.
- `/nextseason` remains available.
- `/stop` cancels the caption batch.

## Video cover features

### Set a cover

Send:

```text
/cover
```

Then send a photo. The bot stores only its Telegram `file_id`.

You can also reply to an existing photo with:

```text
/cover
```

### Cover controls

```text
/cover status
/cover off
```

When a video is processed while a cover is enabled, the bot uses Telegram's `sendVideo` method with:

- the existing video's Telegram `file_id`
- the existing cover photo's Telegram `file_id`

No video download occurs and no video multipart upload occurs.

Non-video media continues through the normal Telegram copy path.

### Important Bot API limitation

The cover path uses Telegram's Bot API `sendVideo`, not `copyMessage`, because `copyMessage` does not expose a custom video-cover parameter. The video itself is still reused by `file_id`; this implementation does not download/re-upload the video.

If Telegram rejects a video-cover operation, that source video is counted as failed and is deliberately **not deleted**.

## Run

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env
python -m caption_bot.main
```
