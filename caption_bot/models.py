from dataclasses import dataclass, field
from typing import Optional
import asyncio


@dataclass(frozen=True, order=True)
class Episode:
    season: int
    episode: int


@dataclass
class BatchItem:
    message_id: int
    chat_id: int
    filename: Optional[str]
    display_name: str
    detected: Optional[Episode] = None
    assigned: Optional[Episode] = None
    media_type: Optional[str] = None
    file_id: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[int] = None
    supports_streaming: bool = False
    has_spoiler: bool = False
    caption: Optional[str] = None


@dataclass
class UserState:
    current_caption: Optional[str] = None

    # Confirmed files waiting for /preview.
    batch: list[BatchItem] = field(default_factory=list)

    preview_message_id: Optional[int] = None
    preview_chat_id: Optional[int] = None

    # Temporary "Added to batch" acknowledgement messages.
    batch_ack_message_ids: list[int] = field(default_factory=list)

    processing: bool = False

    # Saved Telegram cover.
    cover_file_id: Optional[str] = None
    cover_message_id: Optional[int] = None
    awaiting_cover: bool = False

    # ---------------------------------------------------------
    # Incoming burst collection
    # ---------------------------------------------------------
    #
    # Files are collected briefly before being classified.
    # This allows:
    #
    #   S03E01
    #   S03E02
    #   S03E03
    #   ...
    #
    # to be analyzed as ONE batch instead of warning on S03E01
    # and rejecting the following 19 messages.
    #
    incoming_batch: list[BatchItem] = field(default_factory=list)
    collection_task: Optional[asyncio.Task] = field(
        default=None,
        repr=False,
    )

    # ---------------------------------------------------------
    # Batch-level sequence confirmation
    # ---------------------------------------------------------
    #
    # These remain separate from the normal active batch until
    # the user chooses "Yes, continue".
    #
    pending_items: list[BatchItem] = field(default_factory=list)
    pending_valid_items: list[BatchItem] = field(default_factory=list)
    pending_rejected_items: list[BatchItem] = field(default_factory=list)
    pending_review_message_id: Optional[int] = None

    # Text of the last "✅ Batch finished" summary, so a rejected
    # incoming burst (season-change "No") can restore this screen
    # instead of showing a generic cancellation message.
    last_finished_text: Optional[str] = None

    # ---------------------------------------------------------
    # Cover-only mode
    # ---------------------------------------------------------
    #
    # Active when a cover is saved but no /scaption sequence is
    # running: media is just re-sent with the cover applied
    # (videos) or passed through untouched (everything else),
    # original captions preserved, no caption rewriting.
    #
    cover_batch: list[BatchItem] = field(default_factory=list)
    cover_preview_message_id: Optional[int] = None
    cover_collection_task: Optional[asyncio.Task] = field(
        default=None,
        repr=False,
    )

    # ---------------------------------------------------------
    # /transfer season picker
    # ---------------------------------------------------------
    #
    # Seasons currently toggled on in the /transfer inline picker.
    # Cleared once a link is generated or the picker is cancelled.
    #
    transfer_selection: set[int] = field(default_factory=set)
    transfer_message_id: Optional[int] = None
