import requests
from typing import Optional, Tuple, List
from streamonitor.bot import Bot
from streamonitor.enums import Status


class Cam4(Bot):
    site: str = 'Cam4'
    siteslug: str = 'C4'

    def __init__(self, username: str) -> None:
        super().__init__(username)
        self.url = self.getWebsiteURL()

    def get_site_color(self) -> Tuple[str, List[str]]:
        """Return the color scheme for this site."""
        return ("red", [])

    def getWebsiteURL(self) -> str:
        """Get the website URL for this streamer."""
        return f"https://hu.cam4.com/{self.username}"
    
    def getVideoUrl(self) -> Optional[str]:
        """Get the video stream URL."""
        if not self.lastInfo or 'cdnURL' not in self.lastInfo:
            return None
        return self.getWantedResolutionPlaylist(self.lastInfo['cdnURL'])

    # Consecutive blocked responses; only the first is worth a WARNING.
    _consec_block = 0

    def _blockedStatus(self, code: int, what: str) -> Status:
        """Map a non-OK Cam4 HTTP status to a bot Status that actually backs off.

        This used to return Status.UNKNOWN and log a WARNING every poll. UNKNOWN
        gets no backoff, so a persistently 403-ing model re-polled on every
        cycle forever — 25 identical "Stream info check failed with HTTP 403"
        lines dominated the dashboard's event feed and buried real failures.

        403/429 from Cam4 is an exit-IP reputation block, not a per-model
        problem, so RATELIMIT is the right state: it backs off (sleep_on_ratelimit,
        exponential) AND reports to the VPN auto-rotator, which is exactly the
        signal rotation exists to act on.
        """
        self._consec_block += 1
        if code in (403, 429):
            # First one is worth seeing; the rest are the same fact repeated.
            if self._consec_block == 1:
                self.logger.warning(
                    f"{what} blocked (HTTP {code}) — backing off and flagging "
                    f"the exit IP for rotation")
            else:
                self.logger.debug(f"{what} still blocked (HTTP {code}) "
                                  f"x{self._consec_block}")
            return Status.RATELIMIT
        if code >= 500:
            self.logger.debug(f"{what} server error (HTTP {code})")
            return Status.UNKNOWN
        self.logger.debug(f"{what} failed with HTTP {code}")
        return Status.UNKNOWN

    def getStatus(self) -> Status:
        """Check the current status of the stream."""
        try:
            headers = self.headers.copy()
            headers.update({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
            })

            # If not currently streaming, check profile info first
            if self.sc == Status.NOTRUNNING:
                try:
                    profile_response = self.session.get(
                        f'https://hu.cam4.com/rest/v1.0/profile/{self.username}/info',
                        headers=headers,
                        timeout=30,
                        bucket='status'
                    )
                    
                    if profile_response.status_code == 403:
                        return Status.RESTRICTED
                    elif profile_response.status_code != 200:
                        self.logger.warning(f"Profile check failed with HTTP {profile_response.status_code}")
                        return Status.NOTEXIST

                    profile_data = profile_response.json()
                    if not profile_data.get('online', False):
                        return Status.OFFLINE
                        
                except Exception as e:
                    self.logger.error(f"Error checking profile: {e}")
                    return Status.ERROR

            # Check access to room
            try:
                access_response = self.session.get(
                    f'https://webchat.cam4.com/requestAccess?roomname={self.username}',
                    headers=headers,
                    timeout=30,
                    bucket='status'
                )
                
                if access_response.status_code != 200:
                    return self._blockedStatus(access_response.status_code,
                                               "Access check")
                    
                access_data = access_response.json()
                if access_data.get('privateStream', False):
                    return Status.PRIVATE
                    
            except Exception as e:
                self.logger.error(f"Error checking room access: {e}")
                return Status.ERROR

            # Get stream info
            try:
                stream_response = self.session.get(
                    f'https://hu.cam4.com/rest/v1.0/profile/{self.username}/streamInfo',
                    headers=headers,
                    timeout=30,
                    bucket='status'
                )
                
                if stream_response.status_code == 204:
                    return Status.OFFLINE
                elif stream_response.status_code == 200:
                    self.lastInfo = stream_response.json()
                    self._consec_block = 0
                    return Status.PUBLIC
                else:
                    return self._blockedStatus(stream_response.status_code,
                                               "Stream info check")
                    
            except Exception as e:
                self.logger.error(f"Error getting stream info: {e}")
                return Status.ERROR

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Network error checking status: {e}")
            return Status.ERROR
        except (TimeoutError, ConnectionError, OSError) as e:
            # curl_cffi (CFSessionManager) raises a parallel exception
            # tree from requests.exceptions — its TimeoutError /
            # ConnectionError / DNSError all subclass OSError, NOT
            # requests.exceptions.RequestException. Without this catch,
            # transient socket failures fall through to the noisy
            # catch-all and are logged as ERROR instead of RATELIMIT.
            self.logger.debug(f"Network/timeout {type(e).__name__}: {e}")
            return Status.RATELIMIT
        except (KeyError, ValueError) as e:
            self.logger.error(f"Error parsing response: {e}")
            return Status.ERROR
        except Exception as e:
            self.logger.error(f"Unexpected error [{type(e).__name__}]: {e!r}")
            return Status.ERROR
    
    def isMobile(self) -> bool:
        """Check if this is a mobile broadcast."""
        return False
