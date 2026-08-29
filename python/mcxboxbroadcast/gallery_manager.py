"""Gallery manager, mirroring the Java ``GalleryManager``.

Sets the profile showcase image from a local screenshot, comparing image hashes
to avoid re-uploading an unchanged image.
"""

from __future__ import annotations

import zlib
from datetime import datetime, timezone
from typing import Optional

import requests

from mcxboxbroadcast.constants import GALLERY, gson_loads
from mcxboxbroadcast.logger import Logger


class GalleryManager:
    def __init__(self, http_client: requests.Session, logger: Logger, session_manager: "object") -> None:
        self._http_client = http_client
        self._logger = logger.prefixed("Gallery")
        self._session_manager = session_manager

    def set_showcase(self, image_path: str) -> bool:
        existing_images = self.get_images()
        if existing_images is None:
            self._logger.error(
                "Unable to set showcase image, you are likely being rate limited"
            )
            return False

        new_image_id: Optional[str] = None

        if existing_images:
            try:
                with open(image_path, "rb") as handle:
                    new_image_hash = self._image_hash(handle.read())
            except OSError as e:
                self._logger.error(f"Failed to read new showcase image file: {e}")
                return False

            # Compare hashes against existing images
            for image in existing_images:
                try:
                    image_data = self._get_image(image)
                    if image_data is not None and new_image_hash == self._image_hash(image_data):
                        new_image_id = image.get("id")
                        self._logger.info("Showcase image is already set, skipping upload")
                        break
                except Exception as e:
                    self._logger.error(f"Failed to compare showcase image: {e}")

        if new_image_id is None:
            new_image_id = self._upload_image(image_path, True)

        if new_image_id is None:
            return False

        for image in existing_images:
            if image.get("id") == new_image_id:
                continue
            self._delete_image(image.get("id"))

        return True

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _upload_image(self, image_path: str, is_featured: bool) -> Optional[str]:
        try:
            import os

            last_modified = datetime.fromtimestamp(
                os.path.getmtime(image_path), tz=timezone.utc
            ).isoformat().replace("+00:00", "Z")

            with open(image_path, "rb") as handle:
                response = self._http_client.post(
                    GALLERY,
                    headers={
                        "Authorization": self._session_manager.get_mc_token_header(),
                        "content-type": "application/octet-stream",
                        "x-ms-showcased-featured": str(is_featured).lower(),
                        "x-ms-showcased-timetaken": last_modified,
                    },
                    data=handle.read(),
                    timeout=30,
                )

            if response.status_code != 202:
                self._logger.error(f"Failed to upload and set featured gallery image: {response.text}")
                return None

            data = gson_loads(response.text)
            return data.get("result", {}).get("id")
        except Exception as e:
            self._logger.error(f"Failed to upload and set featured gallery image: {e}")
            return None

    def _delete_image(self, image_id: str) -> None:
        try:
            self._http_client.delete(
                f"{GALLERY}/{image_id}",
                headers={"Authorization": self._session_manager.get_mc_token_header()},
                timeout=30,
            )
        except Exception as e:
            self._logger.error(f"Failed to delete gallery image: {e}")

    def get_images(self) -> Optional[list]:
        """Return the list of showcased images, or None on failure."""
        try:
            response = self._http_client.get(
                f"{GALLERY}/xuid/{self._session_manager.get_xuid()}",
                headers={"Authorization": self._session_manager.get_mc_token_header()},
                timeout=30,
            )
            data = gson_loads(response.text)
            result = data.get("result")
            if result is None:
                raise RuntimeError("Gallery response result is null")
            return result.get("showcasedImages") or []
        except Exception as e:
            self._logger.error(f"Failed to get gallery images: {e}")
            return None

    def _get_image(self, image: dict) -> Optional[bytes]:
        try:
            response = self._http_client.get(
                image.get("url"),
                headers={"Authorization": self._session_manager.get_mc_token_header()},
                timeout=30,
            )
            return response.content
        except Exception:
            return None

    @staticmethod
    def _image_hash(data: bytes) -> str:
        """Compute the CRC32 of the raw image data, hex encoded (8 chars)."""
        return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
