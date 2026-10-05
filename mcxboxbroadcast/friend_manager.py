"""Friend manager, ported from Java FriendManager.java."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import requests

from . import constants, http
from .config.core_config import FriendSyncConfig
from .logger import Logger
from .models.session import (
    CreateHandleRequest,
    FriendModifyResponse,
    FriendRequestAcceptResponse,
    FriendRequestResponse,
    FriendStatusResponse,
    FollowerResponse,
    Person,
    SessionRef,
)
from .storage.storage_manager import StorageManager


def _headers(token: str) -> dict:
    return {"Authorization": token}


class FriendManager:
    def __init__(self, http_session: requests.Session, logger: Logger, session_manager) -> None:
        self.httpClient = http_session
        self.logger = logger
        self.sessionManager = session_manager

        self._to_add: dict[str, str] = {}
        self._to_remove: dict[str, str] = {}
        self._to_add_lock = threading.Lock()

        self._last_friend_cache: Optional[list[Person]] = None
        self._internal_scheduled_future: Optional[object] = None
        self._initial_invite = False
        self._should_accept_pending_requests = True

    def get(self) -> list[Person]:
        people: list[Person] = []
        token = self.sessionManager.get_token_header()

        last_response = ""
        try:
            response = http.get(
                self.httpClient,
                constants.FRIENDS,
                {**_headers(token), "x-xbl-contract-version": "7", "accept-language": "en-GB"},
            )
            last_response = response.text
            if last_response:
                friends_response = FollowerResponse.from_json(response.json())
                if friends_response.people is not None:
                    people.extend(friends_response.people)
        except (ValueError, requests.RequestException) as ex:
            self.logger.debug(f"Friends request response: {last_response}")
            raise XboxFriendsError(str(ex))

        self._last_friend_cache = people
        return people

    def add(self, xuid: str, gamertag: str) -> None:
        with self._to_add_lock:
            # Remove the user from the remove list (if they are on it)
            self._to_remove.pop(xuid, None)
            # Add the user to the add list
            self._to_add[xuid] = gamertag
        self._call_internal_process()

    def add_if_required(self, xuid: str, gamertag: str) -> bool:
        with self._to_add_lock:
            if xuid in self._to_add:
                return False

        # Check if we are already friends
        try:
            response = http.get(
                self.httpClient,
                constants.PEOPLE % xuid,
                {**_headers(self.sessionManager.get_token_header()), "x-xbl-contract-version": "3"},
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("isFriend", False):
                    return False
        except (ValueError, requests.RequestException) as ex:
            # Debug log it failed and assume we aren't friends
            self.logger.debug(f"Failed to check if {gamertag} ({xuid}) is a friend: {ex}")

        self.add(xuid, gamertag)
        return True

    def remove(self, xuid: str, gamertag: Optional[str]) -> None:
        # Try and get the gamertag from the cache if it wasn't provided
        if gamertag is None:
            if self._last_friend_cache is not None:
                found = next((p for p in self.last_friend_cache() if p.xuid == xuid), None)
                gamertag = found.gamertag if found else "Unknown"
            else:
                gamertag = "Unknown"

        with self._to_add_lock:
            # Remove the user from the add list (if they are on it)
            self._to_add.pop(xuid, None)
            # Add the user to the remove list
            self._to_remove[xuid] = gamertag
        self._call_internal_process()

    def init(self, friend_sync_config: FriendSyncConfig) -> None:
        self._should_accept_pending_requests = getattr(
            friend_sync_config, "auto_friend", getattr(friend_sync_config, "auto_follow", True)
        )

        # Initialize the auto friend sync if enabled
        self._init_auto_friend(friend_sync_config)

        # Accept any pending friend requests if enabled incase we got any while offline
        self.accept_pending_friend_requests()

        if not friend_sync_config.expiry.enabled:
            return

        player_history = self.sessionManager.storage_manager().player_history()
        if player_history.is_first_run():
            self.logger.info(
                "Player history is being initialized for the first time, this may take a few seconds"
            )
            try:
                for friend in self.get():
                    player_history.last_seen(friend.xuid, datetime.now(timezone.utc))
            except Exception as ex:
                self.logger.error("Failed to initialize player history", ex)
        else:
            try:
                friend_xuids = {p.xuid for p in self.last_friend_cache()}
                history_xuids = set(player_history.all().keys())

                # Remove any players from history that are no longer friends
                for xuid in history_xuids:
                    if xuid not in friend_xuids:
                        player_history.clear(xuid)

                # Add any friends that are missing from history
                for xuid in friend_xuids:
                    if xuid not in history_xuids:
                        player_history.last_seen(xuid, datetime.now(timezone.utc))
            except Exception as ex:
                self.logger.error("Failed to clean up player history", ex)

        def expiry_check() -> None:
            try:
                xuid_gamertag = {
                    p.xuid: p.gamertag for p in self.last_friend_cache()
                }
                for xuid, last_seen in player_history.all().items():
                    cutoff = datetime.now(timezone.utc) - timedelta(
                        days=friend_sync_config.expiry.days
                    )
                    if last_seen < cutoff:
                        try:
                            self.logger.info(f"Removing player {xuid} from friends due to inactivity")
                            self.remove(xuid, xuid_gamertag.get(xuid))
                        except Exception as ex:
                            if str(ex).startswith("429: "):
                                self.logger.warn(
                                    "Rate limited while trying to remove player "
                                    f"{xuid} from friends for inactivity, will try again later"
                                )
                                return
                            self.logger.error(
                                f"Failed to remove player {xuid} from friends for inactivity", ex
                            )
            except Exception as ex:
                self.logger.error("Failed to clean up friends list", ex)

        self.sessionManager.scheduled_thread().schedule_with_fixed_delay(
            expiry_check, 10, friend_sync_config.expiry.check
        )

    def _init_auto_friend(self, friend_sync_config: FriendSyncConfig) -> None:
        self._initial_invite = friend_sync_config.initial_invite
        if getattr(friend_sync_config, "auto_friend", True):
            self.sessionManager.scheduled_thread().schedule_with_fixed_delay(
                self.accept_pending_friend_requests,
                friend_sync_config.update_interval,
                friend_sync_config.update_interval,
            )

    @staticmethod
    def _is_guest_account(xuid) -> bool:
        try:
            return int(xuid) >> 52 == 1
        except (TypeError, ValueError):
            return False

    def _call_internal_process(self) -> None:
        # If we are already running then don't run again
        if self._internal_scheduled_future is not None and not self._internal_scheduled_future.is_done():
            return
        self._internal_scheduled_future = self.sessionManager.scheduled_thread().submit(
            self._internal_process
        )

    def _internal_process(self) -> None:
        retry_after = 0

        # Initialize the cache if it is None
        if self._last_friend_cache is None:
            self._last_friend_cache = []

        # If we have friends to add then add them
        if self._to_add:
            with self._to_add_lock:
                to_process = dict(self._to_add)
            for xuid, gamertag in to_process.items():
                try:
                    response = http.put(
                        self.httpClient,
                        constants.FRIEND % xuid,
                        _headers(self.sessionManager.get_token_header()),
                    )
                    if response.status_code == 200:
                        # The request was successful so remove them from the list
                        with self._to_add_lock:
                            self._to_add.pop(xuid, None)

                        add_response = FriendAddResponse.from_json(response.json())
                        if add_response.is_friend:
                            # Let the user know we added a friend
                            self.logger.info(f"Added {gamertag} ({xuid}) as a friend")
                            self.send_invite(xuid)

                            # Add the user to the cache
                            if not any(p.xuid == xuid for p in self._last_friend_cache):
                                self._last_friend_cache.append(
                                    Person(xuid=xuid, gamertag=gamertag, is_friend=True)
                                )
                        else:
                            # They hadn't sent us a request so we sent them one
                            self.logger.info(f"Sent a friend request to {gamertag} ({xuid})")
                    elif response.status_code == 429:
                        retry_header = response.headers.get("Retry-After")
                        if retry_header is not None:
                            try:
                                retry_after = int(retry_header)
                            except ValueError:
                                pass
                        self.logger.debug(
                            f"Failed to add {gamertag} ({xuid}) as a friend: "
                            f"({response.status_code}) {response.text}"
                        )
                        # Break out of the loop, so we don't try to add more friends
                        break
                    else:
                        modify = None
                        try:
                            modify = FriendModifyResponse.from_json(response.json())
                        except Exception:
                            pass

                        # 1011 - The requested friend operation was forbidden.
                        # 1015 - An invalid request was attempted.
                        # 1028 - The attempted People request was rejected because it would exceed the People list limit.
                        # 1039 - Request could not be completed due to another request taking precedence.
                        # 1049 - Target user privacy settings do not allow friend requests to be received.

                        if modify is not None and modify.code == 1028:
                            self.logger.error(
                                f"Friend list full, unable to add {gamertag} ({xuid}) as a friend"
                            )
                            # Nothing else can be added so clear the list
                            with self._to_add_lock:
                                self._to_add.clear()
                            break
                        elif modify is not None and modify.code in (1011, 1049):
                            with self._to_add_lock:
                                self._to_add.pop(xuid, None)

                            # Decline their friend request so we don't keep trying to accept it
                            try:
                                self.decline_friend_request(xuid)
                            except Exception as ex:
                                self.logger.debug(
                                    f"Failed to decline friend request from {gamertag} ({xuid}): {ex}"
                                )

                            self.logger.warn(
                                f"Unable to add {gamertag} ({xuid}) as a friend due to restrictions on their account"
                            )
                            self.sessionManager.notification_manager().send_friend_restriction_notification(
                                gamertag, xuid
                            )
                        else:
                            self.logger.warn(
                                f"Failed to add {gamertag} ({xuid}) as a friend: "
                                f"({response.status_code}) {response.text}"
                            )
                except Exception as ex:
                    self.logger.error(f"Failed to add {gamertag} ({xuid}) as a friend: {ex}")
                    break

        # If we have friends to remove then remove them
        # Note: This can be run even if add hits the rate limit as it seems to be separate
        if self._to_remove:
            with self._to_add_lock:
                to_remove_process = dict(self._to_remove)
            for xuid, gamertag in to_remove_process.items():
                try:
                    response = http.delete(
                        self.httpClient,
                        constants.FRIEND % xuid,
                        _headers(self.sessionManager.get_token_header()),
                    )
                    if response.status_code in (200, 204):
                        with self._to_add_lock:
                            self._to_remove.pop(xuid, None)
                        self.logger.info(f"Removed {gamertag} ({xuid}) as a friend")
                        try:
                            self.sessionManager.storage_manager().player_history().clear(xuid)
                        except IOError:
                            pass
                        # Remove the user from the cache
                        self._last_friend_cache = [
                            p for p in self._last_friend_cache if p.xuid != xuid
                        ]
                    elif response.status_code == 429:
                        retry_header = response.headers.get("Retry-After")
                        if retry_header is not None:
                            try:
                                retry_after = int(retry_header)
                            except ValueError:
                                pass
                        self.logger.debug(
                            f"Failed to remove {gamertag} ({xuid}) as a friend: "
                            f"({response.status_code}) {response.text}"
                        )
                        break
                    else:
                        self.logger.warn(
                            f"Failed to remove {gamertag} ({xuid}) as a friend: "
                            f"({response.status_code}) {response.text}"
                        )
                except Exception as ex:
                    self.logger.error(f"Failed to remove {gamertag} ({xuid}) as a friend: {ex}")
                    break

        # If we still have friends to add or remove then schedule another run after the retry after time
        with self._to_add_lock:
            remaining = bool(self._to_add) or bool(self._to_remove)
        if remaining:
            self._internal_scheduled_future = self.sessionManager.scheduled_thread().schedule(
                self._internal_process, retry_after
            )

    def decline_friend_request(self, xuid: str) -> None:
        """Decline a friend request from a user."""
        response = http.delete(
            self.httpClient,
            constants.FRIEND % xuid,
            _headers(self.sessionManager.get_token_header()),
        )
        if response.status_code not in (200, 204):
            raise RuntimeError(f"{response.status_code}: {response.text}")

    def last_friend_cache(self) -> list[Person]:
        if self._last_friend_cache is None:
            try:
                self._last_friend_cache = self.get()
            except XboxFriendsError as ex:
                self.logger.error("Failed to get friends from Xbox Live", ex)
                self._last_friend_cache = []
        return self._last_friend_cache

    def accept_pending_friend_requests(self) -> None:
        if not self._should_accept_pending_requests:
            return

        try:
            token = self.sessionManager.get_token_header()
            # Get the pending friend requests
            response = http.get(
                self.httpClient,
                constants.FRIEND_REQUESTS,
                {**_headers(token), "x-xbl-contract-version": "7", "accept-language": "en-GB"},
            )
            friend_request_response = FollowerResponse.from_json(response.json())

            # We got no pending friend requests returned
            if friend_request_response is None or friend_request_response.people is None:
                return

            # Add them through the normal process to handle rate limits
            for person in friend_request_response.people:
                # Make sure we are not targeting a subaccount (eg: split screen)
                if self._is_guest_account(person.xuid):
                    continue

                with self._to_add_lock:
                    already_in_to_add = person.xuid in self._to_add

                if not already_in_to_add:
                    self.add(person.xuid, person.gamertag)
        except Exception as ex:
            self.logger.error("Failed to accept friend requests", ex)

    def send_invite(self, xuid: str) -> None:
        # Only invite if enabled
        if not self._initial_invite:
            return

        try:
            content = CreateHandleRequest(
                1,
                "invite",
                SessionRef(
                    constants.SERVICE_CONFIG_ID,
                    constants.TEMPLATE_NAME,
                    self.sessionManager.get_session_id(),
                ),
                invited_xuid=xuid,
                invite_attributes={"titleId": constants.TITLE_ID},  # Minecraft Windows title Id
            )
            response = http.post_json(
                self.httpClient,
                constants.CREATE_HANDLE,
                {**_headers(self.sessionManager.get_token_header()), "x-xbl-contract-version": "107"},
                content.to_json(),
            )
            self.logger.debug(response.text)
        except requests.RequestException as ex:
            self.logger.error(f"Failed to send invite to {xuid}: {ex}")


class XboxFriendsError(Exception):
    pass


# Keep the exception name used in get()
XboxFriendsException = XboxFriendsError
