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

        # People following us
        last_response = ""
        try:
            response = http.get(
                self.httpClient,
                constants.FOLLOWERS,
                {**_headers(token), "x-xbl-contract-version": "5", "accept-language": "en-GB"},
            )
            last_response = response.text
            if last_response:
                follower_response = FollowerResponse.from_json(response.json())
                if follower_response.people is not None:
                    people.extend(follower_response.people)
        except (ValueError, requests.RequestException) as ex:
            self.logger.debug(f"Follower request response: {last_response}")
            raise XboxFriendsError(str(ex))

        # People we are following
        last_response = ""
        try:
            response = http.get(
                self.httpClient,
                constants.SOCIAL,
                {**_headers(token), "x-xbl-contract-version": "5", "accept-language": "en-GB"},
            )
            last_response = response.text
            if last_response:
                social_response = FollowerResponse.from_json(response.json())
                if social_response.people is not None:
                    people.extend(social_response.people)
        except (ValueError, requests.RequestException) as ex:
            self.logger.debug(f"Social request response: {last_response}")
            raise XboxFriendsError(str(ex))

        # Merge the 2 lists together
        out_people: dict[str, Person] = {}
        for person in people:
            if person.xuid in out_people:
                out_people[person.xuid] = out_people[person.xuid].merge(person)
            else:
                out_people[person.xuid] = person

        out_list = list(out_people.values())
        self._last_friend_cache = out_list
        return out_list

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
                _headers(self.sessionManager.get_token_header()),
            )
            status = FriendStatusResponse.from_json(response.json())
            if status.is_following_caller and status.is_followed_by_caller:
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
                found = next((p for p in self._last_friend_cache if p.xuid == xuid), None)
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
        self._should_accept_pending_requests = friend_sync_config.auto_follow

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
        if friend_sync_config.auto_follow or friend_sync_config.auto_unfollow:

            def sync() -> None:
                try:
                    for person in self.get():
                        # Make sure we are not targeting a subaccount (eg: split screen)
                        if self._is_guest_account(person.xuid):
                            continue

                        # Follow the person back
                        if (
                            friend_sync_config.auto_follow
                            and person.is_following_caller
                            and not person.is_followed_by_caller
                        ):
                            self.add(person.xuid, person.display_name)

                        # Unfollow the person
                        if (
                            friend_sync_config.auto_unfollow
                            and not person.is_following_caller
                            and person.is_followed_by_caller
                        ):
                            self.remove(person.xuid, person.display_name)
                except Exception as ex:
                    self.logger.error("Failed to sync friends", ex)

            self.sessionManager.scheduled_thread().schedule_with_fixed_delay(
                sync,
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

        if self._last_friend_cache is None:
            self._last_friend_cache = []

        # If we have friends to add then add them
        with self._to_add_lock:
            to_process = dict(self._to_add)
        for xuid, gamertag in to_process.items():
            try:
                response = http.put(
                    self.httpClient,
                    constants.PEOPLE % xuid,
                    _headers(self.sessionManager.get_token_header()),
                )
                if response.status_code == 204:
                    with self._to_add_lock:
                        self._to_add.pop(xuid, None)
                    self.logger.info(f"Added {gamertag} ({xuid}) as a friend")
                    self.send_invite(xuid)
                    # Update the user in the cache
                    friend = next(
                        (p for p in self._last_friend_cache if p.xuid == xuid), None
                    )
                    if friend is not None:
                        friend.is_followed_by_caller = True
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
                elif response.status_code == 400:
                    modify = FriendModifyResponse.from_json(response.json())
                    if modify.code == 1028:
                        self.logger.error(
                            f"Friend list full, unable to add {gamertag} ({xuid}) as a friend"
                        )
                        break
                    self.logger.warn(
                        f"Failed to add {gamertag} ({xuid}) as a friend: "
                        f"({response.status_code}) {response.text}"
                    )
                else:
                    modify = FriendModifyResponse.from_json(response.json())

                    # 1011 - The requested friend operation was forbidden.
                    # 1015 - An invalid request was attempted.
                    # 1028 - The attempted People request was rejected because it would exceed the People list limit.
                    # 1039 - Request could not be completed due to another request taking precedence.
                    # 1049 - Target user privacy settings do not allow friend requests to be received.

                    if modify.code == 1028:
                        self.logger.error(
                            f"Friend list full, unable to add {gamertag} ({xuid}) as a friend"
                        )
                    elif modify.code in (1011, 1049):
                        with self._to_add_lock:
                            self._to_add.pop(xuid, None)
                        # Remove these people from following us (block and unblock)
                        try:
                            self.force_unfollow(xuid)
                        except Exception as ex:
                            self.logger.error("Failed to force unfollow user", ex)
                        self.logger.warn(
                            f"Removed {gamertag} ({xuid}) as a friend due to restrictions on their account"
                        )
                        self.sessionManager.notification_manager().send_friend_restriction_notification(
                            gamertag, xuid
                        )
                    else:
                        self.logger.warn(
                            f"Failed to add {gamertag} ({xuid}) as a friend: "
                            f"({response.status_code}) {response.text}"
                        )
            except requests.RequestException as ex:
                self.logger.error(f"Failed to add {gamertag} ({xuid}) as a friend: {ex}")
                break

        # If we have friends to remove then remove them
        # Note: This can be run even if add hits the rate limit as it seems to be separate
        with self._to_add_lock:
            to_remove_process = dict(self._to_remove)
        for xuid, gamertag in to_remove_process.items():
            try:
                response = http.delete(
                    self.httpClient,
                    constants.PEOPLE % xuid,
                    _headers(self.sessionManager.get_token_header()),
                )
                if response.status_code == 204:
                    with self._to_add_lock:
                        self._to_remove.pop(xuid, None)
                    self.logger.info(f"Removed {gamertag} ({xuid}) as a friend")
                    try:
                        self.sessionManager.storage_manager().player_history().clear(xuid)
                    except IOError:
                        pass
                    friend = next(
                        (p for p in self._last_friend_cache if p.xuid == xuid), None
                    )
                    if friend is not None:
                        friend.is_followed_by_caller = False
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
            except requests.RequestException as ex:
                self.logger.error(f"Failed to remove {gamertag} ({xuid}) as a friend: {ex}")
                break

        # If we still have friends to add or remove then schedule another run after the retry after time
        with self._to_add_lock:
            remaining = bool(self._to_add) or bool(self._to_remove)
        if remaining:
            self._internal_scheduled_future = self.sessionManager.scheduled_thread().schedule(
                self._internal_process, retry_after
            )

    def force_unfollow(self, xuid: str) -> None:
        response = http.delete(
            self.httpClient,
            constants.FOLLOWER % xuid,
            _headers(self.sessionManager.get_token_header()),
        )
        if response.status_code == 204:
            # Remove the user from the cache
            if self._last_friend_cache is not None:
                self._last_friend_cache = [p for p in self._last_friend_cache if p.xuid != xuid]
            try:
                self.sessionManager.storage_manager().player_history().clear(xuid)
            except IOError:
                pass
        else:
            raise Exception(f"{response.status_code}: {response.text}")

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
                "https://peoplehub.xboxlive.com/users/me/people/friendrequests(received)",
                {**_headers(token), "x-xbl-contract-version": "7", "accept-language": "en-GB"},
            )
            friend_request_response = FriendRequestResponse.from_json(response.json())

            # We got no pending friend requests returned
            if friend_request_response.people is None:
                return

            xuids = [p.xuid for p in friend_request_response.people]

            # Don't try and accept if there are no requests
            if not xuids:
                return

            accepted_xuids: list[str] = []

            # Accept the friend requests, bulk seemed to have issues so 1 by 1
            for xuid in xuids:
                accept_response = http.put(
                    self.httpClient,
                    f"https://social.xboxlive.com/users/me/people/friends/v2/xuid({xuid})",
                    _headers(token),
                )
                accept = FriendRequestAcceptResponse.from_json(accept_response.json())
                if accept.is_friend:
                    accepted_xuids.append(xuid)

            # If we don't have any updated people then we don't need to do anything else
            if not accepted_xuids:
                return

            # Let the user know we accepted the friend requests
            for xuid in accepted_xuids:
                friend = next(
                    (p for p in friend_request_response.people or [] if p.xuid == xuid), None
                )
                if friend is None:
                    continue
                self.logger.info(f"Added {friend.gamertag} ({xuid}) as a friend")
                self.send_invite(xuid)
        except (ValueError, requests.RequestException) as ex:
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
