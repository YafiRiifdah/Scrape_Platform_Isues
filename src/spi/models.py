"""Model data ternormalisasi lintas platform (Instagram & TikTok)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Platform = Literal["instagram", "tiktok"]
MediaType = Literal["image", "video", "carousel", "unknown"]


def to_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalisasi datetime ke UTC. Datetime naive dianggap sudah UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Post(BaseModel):
    platform: Platform
    account: str
    post_id: str
    url: str
    created_at: datetime
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list)
    media_type: MediaType = "unknown"
    likes: Optional[int] = None
    comments_count: Optional[int] = None
    shares: Optional[int] = None
    views: Optional[int] = None
    raw: Optional[dict[str, Any]] = None

    @field_validator("created_at")
    @classmethod
    def _created_at_utc(cls, v: datetime) -> datetime:
        return to_utc(v)


class Comment(BaseModel):
    platform: Platform
    post_id: str
    comment_id: str
    text: str
    created_at: Optional[datetime] = None
    likes: Optional[int] = None
    is_reply: bool = False
    parent_comment_id: Optional[str] = None
    replies_count: Optional[int] = None  # jumlah balasan; None = tidak diketahui (dihitung dari balasan tersimpan)

    @field_validator("created_at")
    @classmethod
    def _created_at_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        return to_utc(v)
