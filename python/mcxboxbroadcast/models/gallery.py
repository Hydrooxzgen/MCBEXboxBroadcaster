"""Gallery models, mirroring ``core/models/gallery``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class GalleryImage:
    id: Optional[str] = None
    isFeatured: bool = False
    lastModified: Optional[str] = None
    takenTime: Optional[str] = None
    url: Optional[str] = None


@dataclass
class GalleryResponse:
    result: Optional["GalleryResponse.Result"] = None

    @dataclass
    class Result:
        showcasedImages: Optional[list] = None  # list[GalleryImage]


@dataclass
class GalleryUploadResponse:
    result: Optional[GalleryImage] = None


__all__ = ["GalleryImage", "GalleryResponse", "GalleryUploadResponse"]
