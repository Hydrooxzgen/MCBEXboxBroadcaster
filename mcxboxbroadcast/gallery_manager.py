"""Gallery manager, ported from Java GalleryManager.java (showcase image)."""

from __future__ import annotations

import io
import zlib
from datetime import datetime, timezone
from typing import Optional

import requests

from . import constants, http
from .logger import Logger
from .models.gallery import GalleryResponse, GalleryUploadResponse


class GalleryManager:
    def __init__(self, http_session: requests.Session, logger: Logger, session_manager) -> None:
        self.httpClient = http_session
        self.logger = logger
        self.sessionManager = session_manager

    def set_showcase(self, image_data: Optional[bytes], last_modified: float = 0.0) -> bool:
        if image_data is None:
            return False
        existing_images = self._get_images()
        if existing_images is None:
            self.logger.error(
                "Unable to set showcase image, you are likely being rate limited"
            )
            return False

        new_image_id: Optional[str] = None

        if len(existing_images) >= 1:
            # Check if the image is already set as the showcase image by comparing the hash
            try:
                new_image_hash = self._get_image_hash(image_data)
                for image in existing_images:
                    image_data_existing = self._get_image(image.url)
                    image_hash = self._get_image_hash(image_data_existing)
                    if new_image_hash == image_hash:
                        new_image_id = image.id
                        self.logger.info("Showcase image is already set, skipping upload")
                        break
            except IOError as ex:
                self.logger.error("Failed to read new showcase image file", ex)
                return False

        if new_image_id is None:
            new_image_id = self._upload_image(image_data, True, last_modified)

        if new_image_id is None:
            return False

        for image in existing_images:
            if image.id != new_image_id:
                self._delete_image(image.id)

        return True

    def _upload_image(self, image_data: bytes, is_featured: bool, last_modified: float) -> Optional[str]:
        try:
            taken_time = datetime.fromtimestamp(last_modified, tz=timezone.utc).isoformat()
            response = http.post_raw(
                self.httpClient,
                constants.GALLERY,
                {
                    "Authorization": self.sessionManager.get_mc_token_header(),
                    "content-type": "application/octet-stream",
                    "x-ms-showcased-featured": str(bool(is_featured)).lower(),
                    "x-ms-showcased-timetaken": taken_time,
                },
                image_data,
            )
            if response.status_code != 202:
                self.logger.error(
                    f"Failed to upload and set featured gallery image: {response.text}"
                )
                return None
            upload = GalleryUploadResponse.from_json(response.json())
            return upload.result.id if upload.result else None
        except (ValueError, requests.RequestException) as ex:
            self.logger.error("Failed to upload and set featured gallery image", ex)
        return None

    def _delete_image(self, image_id: str) -> None:
        try:
            http.delete(
                self.httpClient,
                f"{constants.GALLERY}/{image_id}",
                {"Authorization": self.sessionManager.get_mc_token_header()},
            )
        except requests.RequestException as ex:
            self.logger.error("Failed to delete gallery image", ex)

    def _get_images(self) -> Optional[list]:
        try:
            response = http.get(
                self.httpClient,
                f"{constants.GALLERY}/xuid/{self.sessionManager.get_xuid()}",
                {"Authorization": self.sessionManager.get_mc_token_header()},
            )
            gallery = GalleryResponse.from_json(response.json())
            if gallery.result is None:
                raise RuntimeError("Gallery response result is null")
            return gallery.result.showcased_images
        except (ValueError, RuntimeError, requests.RequestException) as ex:
            self.logger.error("Failed to get gallery images", ex)
        return None

    def _get_image(self, url: str) -> Optional[bytes]:
        try:
            response = self.httpClient.get(
                url,
                headers={"Authorization": self.sessionManager.get_mc_token_header()},
                timeout=10,
            )
            return response.content
        except requests.RequestException:
            return None

    @staticmethod
    def _get_image_hash(image_data: Optional[bytes]) -> str:
        if image_data is None:
            return ""
        digest = zlib.crc32(image_data) & 0xFFFFFFFF
        return f"{digest:08x}"
