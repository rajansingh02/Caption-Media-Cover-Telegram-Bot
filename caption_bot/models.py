from dataclasses import dataclass, field
from typing import Optional


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


@dataclass
class UserState:
    current_caption: Optional[str] = None
    batch: list[BatchItem] = field(default_factory=list)
    preview_message_id: Optional[int] = None
    preview_chat_id: Optional[int] = None
    processing: bool = False
    cover_file_id: Optional[str] = None
    cover_message_id: Optional[int] = None
    awaiting_cover: bool = False
