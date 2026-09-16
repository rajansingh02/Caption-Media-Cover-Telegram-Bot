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

## Tests

```bash
python -m pytest tests -q
```

---

# Running on low-end hardware

The bot is tuned to run on a single core with very little RAM. Nothing needs
configuring — every knob already defaults to the small-machine setting — but
they are all exposed in `.env` if you want to change them.

## Tuning knobs (`.env`)

| Variable | Default | What it does |
| --- | --- | --- |
| `WORKERS` | `1` | Pyrogram update-dispatcher tasks. Pyrogram's own default is `min(32, cpu_count + 4)`, which is pointless for a single-user bot. |
| `MAX_CONCURRENT_TRANSMISSIONS` | `1` | Parallel up/download slots. Nothing is ever transferred byte-by-byte here, so the slots were pure overhead. |
| `COLLECTION_DELAY` | `2.5` | Quiet period used to group an incoming media burst into one batch. |
| `SEND_DELAY` | `1.2` | Gap between files while `/transfer` resends a season. |
| `LOG_LEVEL` | `INFO` | Set `WARNING` to cut console/disk logging to almost nothing (worth it on SD cards). |
| `USE_UVLOOP` | `true` | Uses uvloop **if it is installed**; silently ignored otherwise. |
| `SESSION_IN_MEMORY` | `false` | Keeps the Pyrogram session in RAM — no session-file writes, but the peer cache is rebuilt on each restart. |
| `DB_CACHE_KIB` | `2048` | SQLite page cache for the archive. |
| `MAX_BATCH_SIZE` | `100` | Unchanged. |

Optional extra speed, if a wheel is available for your device:

```bash
pip install uvloop
```

## What was optimised

Behaviour is unchanged. The planner, burst classification, review text,
preview text and keyboards were verified byte-for-byte identical to the
previous implementation across ~70,000 randomised filenames and 8,000
randomised batch scenarios.

**Fewer Telegram API round trips**

- Source and acknowledgement messages are deleted in grouped calls (up to
  100 IDs per request, per chat) instead of one request per message. A
  finished 20-file batch drops from 20+ delete requests to 1. If a grouped
  call fails, the chunk is retried one message at a time, so the reported
  "deleted"/"failed" counts stay exact.

**One TLS handshake instead of one per video**

- The cover path kept a fresh `urlopen()` per video: DNS lookup plus a TLS
  handshake each time, which was the single most expensive thing the bot
  did on a small ARM board. It now holds one keep-alive HTTPS connection
  and runs requests on one dedicated worker thread, instead of borrowing
  from asyncio's default pool (which sizes itself to `min(32, cpu + 4)`
  threads and keeps them alive). A dropped socket is rebuilt and retried
  transparently.

**Less disk work in the archive**

- `synchronous=NORMAL` under WAL removes an `fsync` per commit, and a
  finished batch is now written in one transaction instead of one commit
  per episode: 200 episodes went from ~33 ms to ~0.3 ms on a fast disk, and
  the gap is far wider on SD cards. WAL still protects the database against
  a process crash.
- The `/transfer` picker ran one `SELECT` plus one `COUNT(*)` **per season,
  every time a button was tapped**. It is one grouped query now.
- Seasons are read a page at a time while sending instead of materialising
  every record up front.

**Cheaper hot paths**

- `detect_episode()` runs for every file, again per re-analysis, again per
  preview refresh and again during processing. Marker pairing is now
  O(S + E) with a single-marker fast path instead of building a tuple per
  (season, episode) combination and sorting the lot, and results are
  memoised per string (512 entries). Measured: ~8x on repeated names
  (the real usage pattern), ~2x on marker-dense names, no regression on
  cold unique names. `replace_episode()` is ~3.7x faster.
- The confirmed batch is planned once; `confirm` used to run `plan_batch`
  and then `_process_batch` ran it again over the same items.
- Review screens used `if item in rejected` against a list of dataclasses,
  running `__eq__` over 14 fields per comparison. Those are identity set
  lookups now.
- Text is assembled once per screen with a single `join`, and the inline
  keyboards are built once instead of on every redraw (a 100-file preview
  keyboard is ~200 button objects, previously rebuilt on every ⬆️/⬇️).

**Lower memory**

- `slots=True` on the dataclasses: `BatchItem` 232 → 144 bytes (a 100-file
  batch: ~23 KB → ~14 KB), `UserState` 344 → 200 bytes.
- Per-user state is no longer created-and-kept for every Telegram ID that
  ever messages the bot; a state that holds nothing is released again.
- `commands.py` no longer re-imports `media.py`, `pyrogram.types` and
  config on every `/preview`.

## Fixed along the way

- **Blocking call on the event loop.** `cover.py` used `urllib`'s blocking
  `urlopen`; it now runs off-loop on its own thread.
- **`S001` lost its width.** `S001E009` used to be rewritten as `S02E12`
  instead of `S002E012`. This is what `tests/test_sequence.py::test_replace_three_digit_width`
  always expected — that test was failing before and passes now.
- **Deadlock risk** in the streamed archive read (the SQLite lock was held
  across `await`s); it uses lock-safe pagination instead.
