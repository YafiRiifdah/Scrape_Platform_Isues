"""Skema structured output untuk analisis per post (tahap 1, Claude Haiku)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Stance = Literal["pro", "kontra", "netral"]
SENTIMENT_KEYS = ("pro", "kontra", "netral")


class RepresentativeComment(BaseModel):
    text: str = Field(description="Kutipan komentar apa adanya, maksimal 200 karakter")
    stance: Stance = Field(description="Sikap komentar terhadap isi postingan")


class DiscussionTrigger(BaseModel):
    text: str = Field(description="Kutipan komentar yang memicu diskusi, maksimal 200 karakter")
    replies_count: int = Field(description="Jumlah balasan komentar tersebut, dari data")
    why: str = Field(description="Satu kalimat: kenapa komentar ini memicu perdebatan")


class PostAnalysis(BaseModel):
    topics: list[str] = Field(
        min_length=1,
        max_length=3,
        description="1-3 topik utama postingan, frasa pendek Bahasa Indonesia, huruf kecil",
    )
    issue_summary: str = Field(description="1-2 kalimat: isu apa yang diangkat postingan ini")
    sentiment: dict[str, float] = Field(
        description="Proporsi komentar pro, kontra, netral terhadap isi postingan; jumlah 1.0"
    )
    public_reaction_summary: str = Field(description="2-3 kalimat ringkasan reaksi netizen di komentar")
    representative_comments: list[RepresentativeComment] = Field(
        max_length=3, description="Maksimal 3 komentar yang paling mewakili reaksi publik"
    )
    intensity: Literal["rendah", "sedang", "tinggi"] = Field(
        description="Seberapa panas perdebatan di komentar"
    )
    discussion_triggers: list[DiscussionTrigger] = Field(
        default_factory=list,
        max_length=2,
        description="0-2 komentar dengan balasan terbanyak yang paling memicu diskusi; kosong bila tidak ada balasan",
    )

    @field_validator("sentiment")
    @classmethod
    def _sentiment_valid(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != set(SENTIMENT_KEYS):
            raise ValueError(f"sentiment harus punya kunci {SENTIMENT_KEYS}")
        total = sum(v.values())
        if not 0.97 <= total <= 1.03:
            raise ValueError("proporsi sentiment harus berjumlah 1.0")
        return v
