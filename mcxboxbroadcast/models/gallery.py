"""Gallery models, ported from the Java core.models.gallery package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class GalleryImage:
    id: str
    is_featured: bool
    last_modified: str
    taken_time: str
    url: str

    @classmethod
    def from_json(cls, json_data: dict) -> "GalleryImage":
        return cls(
            id=str(json_data.get("id", "") or ""),
            is_featured=bool(json_data.get("isFeatured", False)),
            last_modified=str(json_data.get("lastModified", "") or ""),
            taken_time=str(json_data.get("takenTime", "") or ""),
            url=str(json_data.get("url", "") or ""),
        )


@dataclass
class GalleryResponse:
    result: Optional["GalleryResult"]

    @classmethod
    def from_json(cls, json_data: dict) -> "GalleryResponse":
        result = json_data.get("result")
        return cls(
            result=GalleryResult.from_json(result) if result is not None else None
        )


@dataclass
class GalleryResult:
    showcased_images: Optional[list[GalleryImage]]

    @classmethod
    def from_json(cls, json_data: dict) -> "GalleryResult":
        images = json_data.get("showcasedImages")
        return cls(
            showcased_images=[GalleryImage.from_json(i) for i in images]
            if images is not None
            else None
        )


@dataclass
class GalleryUploadResponse:
    result: Optional[GalleryImage]

    @classmethod
    def from_json(cls, json_data: dict) -> "GalleryUploadResponse":
        result = json_data.get("result")
        return cls(result=GalleryImage.from_json(result) if result else None)
