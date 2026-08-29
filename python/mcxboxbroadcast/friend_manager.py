"""Friend manager, mirroring the Java ``FriendManager``.

Handles adding/removing friends, auto follow/unfollow syncing, pending friend
request acceptance, invites and inactivity expiry.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import requests

from mcxboxbroadcast.config.core_config import FriendSyncConfig
from mcxboxbroadcast.constants import (
    CREATE_HANDLE,
    FOLLOWER,
    FOLLOWERS,
    MAX_FRIENDS,
    PEOPLE,
    SOCIAL,
    SERVICE_CONFIG_ID,
    TEMPLATE_NAME,
    TITLE_ID,
    gson_loads,
)
from mcxboxbroadcast.exceptions import XboxFriendsException
from mcxboxbroadcast.logger import Logger
from mcxboxbroadcast.models.friend import FriendModifyResponse, FriendRequestResponse
from mcxboxbroadcast.models.session import (
    CreateHandleRequest,
    FollowerResponse,
    SessionRef,
)

FRIEND_REQUESTS_URL = "https://peoplehub.xboxlive.com/users/me/people/friendrequests(received)"
FRIEND_ACCEPT_URL = "https://social.xboxlive.com/users/me/people/friends/v2/xuid(%s)"


class FriendManager:
    def __init__(self, http_client: requests.Session, logger: Logger, session_manager: "object") -> None:
        self._http_client = http_client
        self._logger = logger.prefixed("Friends")
        self._session_manager = session_manager

        self._to_add: dict[str, str] = {}
        self._to_remove: dict[str, str] = {}

        self._last_friend_cache: Optional[list] = None
        self._internal_scheduled_future = None
        self._initial_invite = False
        self._should_accept_pending_requests = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get(self) -> list:
        """Get the merged list of followers + social (friends)."""
        people: list = []

        # Followers
        last_response = ""
        try:
            response = self._http_client.get(
                FOLLOWERS,
                headers={
                    "Authorization": self._session_manager.get_token_header(),
                    "x-xbl-contract-version": "5",
                    "accept-language": "en-GB",
                },
                timeout=30,
            )
            last_response = response.text
            if last_response:
                data = gson_loads(last_response)
                if data.get("people"):
                    people.extend(self._parse_people(data))
        except Exception as e:
            self._logger.debug(f"Follower request response: {last_response}")
            raise XboxFriendsException(str(e)) from e

        # Social
        last_response = ""
        try:
            response = self._http_client.get(
                SOCIAL,
                headers={
                    "Authorization": self._session_manager.get_token_header(),
                    "x-xbl-contract-version": "5",
                    "accept-language": "en-GB",
                },
                timeout=30,
            )
            last_response = response.text
            if last_response:
                data = gson_loads(last_response)
                if data.get("people"):
                    people.extend(self._parse_people(data))
        except Exception as e:
            self._logger.debug(f"Social request response: {last_response}")
            raise XboxFriendsException(str(e)) from e

        # Merge the two lists together
        out_people: dict[str, FollowerResponse.Person] = {}
        for person in people:
            if person.xuid in out_people:
                out_people[person.xuid] = out_people[person.xuid].merge(person)
            else:
                out_people[person.xuid] = person

        out_list = list(out_people.values())
        self._last_friend_cache = out_list
        return out_list

    def add(self, xuid: str, gamertag: str) -> None:
        self._to_remove.pop(xuid, None)
        self._to_add[xuid] = gamertag
        self._call_internal_process()

    def add_if_required(self, xuid: str, gamertag: str) -> bool:
        """Add a friend if they aren't already one. Returns True if added."""
        if xuid in self._to_add:
            return False

        try:
            response = self._http_client.get(
                PEOPLE % xuid,
                headers={"Authorization": self._session_manager.get_token_header()},
                timeout=30,
            )
            data = gson_loads(response.text)
            if data.get("isFollowingCaller") and data.get("isFollowedByCaller"):
                return False
        except Exception as e:
            self._logger.debug(f"Failed to check if {gamertag} ({xuid}) is a friend: {e}")

        self.add(xuid, gamertag)
        return True

    def remove(self, xuid: str, gamertag: Optional[str]) -> None:
        if gamertag is None:
            found = next(
                (person for person in (self._last_friend_cache or []) if person.xuid == xuid),
                None,
            )
            gamertag = found.gamertag if found else "Unknown"

        self._to_add.pop(xuid, None)
        self._to_remove[xuid] = gamertag
        self._call_internal_process()

    def init(self, friend_sync_config: FriendSyncConfig) -> None:
        self._should_accept_pending_requests = friend_sync_config.auto_follow

        # Initialize the auto friend sync if enabled
        self._init_auto_friend(friend_sync_config)

        # Accept any pending friend requests if enabled
        self.accept_pending_friend_requests()

        if not friend_sync_config.expiry.enabled:
            return

        player_history = self._session_manager.storage_manager().player_history()
        if player_history.is_first_run():
            self._logger.info(
                "Player history is being initialized for the first time, this may take a few seconds"
            )
            try:
                for friend in self.get():
                    player_history.last_seen(friend.xuid, datetime.now(timezone.utc))
            except Exception as e:
                self._logger.error(f"Failed to initialize player history: {e}")
        else:
            try:
                friend_xuids = {person.xuid for person in self.last_friend_cache()}
                history_xuids = set(player_history.all().keys())

                # Remove any players from history that are no longer friends
                for xuid in history_xuids:
                    if xuid not in friend_xuids:
                        player_history.clear(xuid)

                # Add any friends that are missing from history
                for xuid in friend_xuids:
                    if xuid not in history_xuids:
                        player_history.last_seen(xuid, datetime.now(timezone.utc))
            except Exception as e:
                self._logger.error(f"Failed to clean up player history: {e}")

        # Schedule the inactivity cleanup
        self._session_manager.scheduled_thread().schedule_with_fixed_delay(
            self._cleanup_inactive, 10, friend_sync_config.expiry.check
        )

    def last_friend_cache(self) -> list:
        if self._last_friend_cache is None:
            try:
                self._last_friend_cache = self.get()
            except XboxFriendsException as e:
                self._logger.error(f"Failed to get friends from Xbox Live: {e}")
                self._last_friend_cache = []
        return self._last_friend_cache

    def accept_pending_friend_requests(self) -> None:
        if not self._should_accept_pending_requests:
            return

        try:
            response = self._http_client.get(
                FRIEND_REQUESTS_URL,
                headers={
                    "Authorization": self._session_manager.get_token_header(),
                    "x-xbl-contract-version": "7",
                    "accept-language": "en-GB",
                },
                timeout=30,
            )
            data = gson_loads(response.text)
            people = data.get("people")
            if not people:
                return

            xuids = [person["xuid"] for person in people]
            accepted_xuids = []

            # Accept the friend requests 1 by 1
            for xuid in xuids:
                accept_response = self._http_client.put(
                    FRIEND_ACCEPT_URL % xuid,
                    headers={"Authorization": self._session_manager.get_token_header()},
                    timeout=30,
                )
                accept_data = gson_loads(accept_response.text)
                if accept_data.get("isFriend"):
                    accepted_xuids.append(xuid)

            if not accepted_xuids:
                return

            for xuid in accepted_xuids:
                friend = next((p for p in people if p["xuid"] == xuid), None)
                if friend is None:
                    continue
                gamertag = friend.get("gamertag") or friend.get("displayName") or "Unknown"
                self._logger.info(f"Added {gamertag} ({xuid}) as a friend")
                self.send_invite(xuid)
        except Exception as e:
            self._logger.error(f"Failed to accept friend requests: {e}")

    def send_invite(self, xuid: str) -> None:
        if not self._initial_invite:
            return

        try:
            content = CreateHandleRequest(
                version=1,
                type="invite",
                sessionRef=SessionRef(SERVICE_CONFIG_ID, TEMPLATE_NAME, self._session_manager.get_session_id()),
                invitedXuid=xuid,
                inviteAttributes={"titleId": TITLE_ID},
            )
            response = self._http_client.post(
                CREATE_HANDLE,
                headers={
                    "Authorization": self._session_manager.get_token_header(),
                    "x-xbl-contract-version": "107",
                },
                json={"version": content.version, "type": content.type, "sessionRef": content.sessionRef.__dict__, "invitedXuid": content.invitedXuid, "inviteAttributes": content.inviteAttributes},
                timeout=30,
            )
            self._logger.debug(response.text)
        except Exception as e:
            self._logger.error(f"Failed to send invite to {xuid}: {e}")

    def force_unfollow(self, xuid: str) -> None:
        """Force a user to unfollow us by blocking/unblocking them."""
        response = self._http_client.delete(
            FOLLOWER % xuid,
            headers={"Authorization": self._session_manager.get_token_header()},
            timeout=30,
        )
        if response.status_code == 204:
            if self._last_friend_cache is not None:
                self._last_friend_cache = [
                    person for person in self._last_friend_cache if person.xuid != xuid
                ]
            self._session_manager.storage_manager().player_history().clear(xuid)
        else:
            raise RuntimeError(f"{response.status_code}: {response.text}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _init_auto_friend(self, friend_sync_config: FriendSyncConfig) -> None:
        self._initial_invite = friend_sync_config.initial_invite
        if friend_sync_config.auto_follow or friend_sync_config.auto_unfollow:
            config = friend_sync_config
            self._session_manager.scheduled_thread().schedule_with_fixed_delay(
                lambda: self._sync_friends(config),
                config.update_interval,
                config.update_interval,
            )

    def _sync_friends(self, config: FriendSyncConfig) -> None:
        try:
            for person in self.get():
                # Make sure we are not targeting a subaccount (eg: split screen)
                if self._is_guest_account(person.xuid):
                    continue

                # Follow the person back
                if config.auto_follow and person.isFollowingCaller and not person.isFollowedByCaller:
                    self.add(person.xuid, person.displayName or person.gamertag or "Unknown")

                # Unfollow the person
                if config.auto_unfollow and not person.isFollowingCaller and person.isFollowedByCaller:
                    self.remove(person.xuid, person.displayName or person.gamertag or "Unknown")
        except Exception as e:
            self._logger.error(f"Failed to sync friends: {e}")

    def _cleanup_inactive(self) -> None:
        try:
            config = self._session_manager.friend_sync_config()
            if config is None or not config.expiry.enabled:
                return
            player_history = self._session_manager.storage_manager().player_history()
            expiry_seconds = config.expiry.days * 86400

            for xuid, last_seen in player_history.all().items():
                if time.time() - last_seen.timestamp() > expiry_seconds:
                    try:
                        self._logger.info(f"Removing player {xuid} from friends due to inactivity")
                        self.remove(xuid, None)
                    except Exception as e:
                        if str(e).startswith("429: "):
                            self._logger.warn(
                                f"Rate limited while trying to remove player {xuid} from friends for inactivity, will try again later"
                            )
                            return
                        self._logger.error(
                            f"Failed to remove player {xuid} from friends for inactivity: {e}"
                        )
        except Exception as e:
            self._logger.error(f"Failed to clean up friends list: {e}")

    @staticmethod
    def _is_guest_account(xuid: str) -> bool:
        try:
            return int(xuid) >> 52 == 1
        except (ValueError, TypeError):
            return False

    def _call_internal_process(self) -> None:
        # If we are already running then don't run again
        if self._internal_scheduled_future is not None and not self._internal_scheduled_future.done():
            return
        self._internal_scheduled_future = self._session_manager.scheduled_thread().submit(
            self._internal_process
        )

    def _internal_process(self) -> None:
        retry_after = 0

        if self._last_friend_cache is None:
            self._last_friend_cache = []

        # -- adds ----------------------------------------------------------
        if self._to_add:
            to_process = dict(self._to_add)
            for xuid, gamertag in to_process.items():
                try:
                    response = self._http_client.put(
                        PEOPLE % xuid,
                        headers={"Authorization": self._session_manager.get_token_header()},
                        timeout=30,
                    )
                    if response.status_code == 204:
                        self._to_add.pop(xuid, None)
                        self._logger.info(f"Added {gamertag} ({xuid}) as a friend")
                        self.send_invite(xuid)

                        friend = next(
                            (p for p in self._last_friend_cache if p.xuid == xuid), None
                        )
                        if friend is not None:
                            friend.isFollowedByCaller = True
                    elif response.status_code == 429:
                        retry_after = self._retry_after(response)
                        self._logger.debug(
                            f"Failed to add {gamertag} ({xuid}) as a friend: ({response.status_code}) {response.text}"
                        )
                        break
                    elif response.status_code == 400:
                        modify = self._parse_modify_response(response)
                        if modify and modify.code == 1028:
                            self._logger.error(
                                f"Friend list full, unable to add {gamertag} ({xuid}) as a friend"
                            )
                            break
                        self._logger.warn(
                            f"Failed to add {gamertag} ({xuid}) as a friend: ({response.status_code}) {response.text}"
                        )
                    else:
                        modify = self._parse_modify_response(response)
                        code = modify.code if modify else None
                        if code == 1028:
                            self._logger.error(
                                f"Friend list full, unable to add {gamertag} ({xuid}) as a friend"
                            )
                        elif code in (1011, 1049):
                            # Restrictions on their account: remove from list and force unfollow
                            self._to_add.pop(xuid, None)
                            try:
                                self.force_unfollow(xuid)
                            except Exception as e:
                                self._logger.error(f"Failed to force unfollow user: {e}")
                            self._logger.warn(
                                f"Removed {gamertag} ({xuid}) as a friend due to restrictions on their account"
                            )
                            self._session_manager.notification_manager().send_friend_restriction_notification(
                                gamertag, xuid
                            )
                        else:
                            self._logger.warn(
                                f"Failed to add {gamertag} ({xuid}) as a friend: ({response.status_code}) {response.text}"
                            )
                except Exception as e:
                    self._logger.error(f"Failed to add {gamertag} ({xuid}) as a friend: {e}")
                    break

        # -- removes -------------------------------------------------------
        if self._to_remove:
            to_process = dict(self._to_remove)
            for xuid, gamertag in to_process.items():
                try:
                    response = self._http_client.delete(
                        PEOPLE % xuid,
                        headers={"Authorization": self._session_manager.get_token_header()},
                        timeout=30,
                    )
                    if response.status_code == 204:
                        self._to_remove.pop(xuid, None)
                        self._logger.info(f"Removed {gamertag} ({xuid}) as a friend")
                        self._session_manager.storage_manager().player_history().clear(xuid)

                        friend = next(
                            (p for p in self._last_friend_cache if p.xuid == xuid), None
                        )
                        if friend is not None:
                            friend.isFollowedByCaller = False
                    elif response.status_code == 429:
                        retry_after = self._retry_after(response)
                        self._logger.debug(
                            f"Failed to remove {gamertag} ({xuid}) as a friend: ({response.status_code}) {response.text}"
                        )
                        break
                    else:
                        self._logger.warn(
                            f"Failed to remove {gamertag} ({xuid}) as a friend: ({response.status_code}) {response.text}"
                        )
                except Exception as e:
                    self._logger.error(f"Failed to remove {gamertag} ({xuid}) as a friend: {e}")
                    break

        # Schedule another run if there is still work to do
        if self._to_add or self._to_remove:
            self._internal_scheduled_future = self._session_manager.scheduled_thread().schedule(
                self._internal_process, retry_after
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _retry_after(response) -> int:
        try:
            return int(response.headers.get("Retry-After", "0"))
        except ValueError:
            return 0

    @staticmethod
    def _parse_modify_response(response) -> Optional[FriendModifyResponse]:
        try:
            data = gson_loads(response.text)
            return FriendModifyResponse(
                code=data.get("code", 0),
                description=data.get("description"),
                source=data.get("source"),
                traceInformation=data.get("traceInformation"),
            )
        except Exception:
            return None

    @staticmethod
    def _parse_people(data: dict) -> list:
        """Convert raw people JSON into :class:`FollowerResponse.Person` objects."""
        people = []
        for item in data.get("people", []):
            person = FollowerResponse.Person()
            for key, value in item.items():
                if hasattr(person, key):
                    setattr(person, key, value)
            people.append(person)
        return people
