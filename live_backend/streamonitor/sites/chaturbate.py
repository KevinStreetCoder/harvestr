import re
import requests
from typing import Optional, Set
from streamonitor.bot import Bot
from streamonitor.enums import Status, Gender


class Chaturbate(Bot):
    site: str = 'Chaturbate'
    siteslug: str = 'CB'
    bulk_update: bool = True

    _GENDER_MAP = {
        'f': Gender.FEMALE,
        'm': Gender.MALE,
        's': Gender.TRANS,
        'c': Gender.BOTH,
    }

    def __init__(self, username: str) -> None:
        super().__init__(username)
        self.vr = False  # Chaturbate doesn't have VR
        self.sleep_on_offline = 30
        self.sleep_on_error = 60
        self._max_consecutive_errors = 50  # Chaturbate can be flaky, allow more retries
        self.url = self.getWebsiteURL()
    
    def get_site_color(self):
        """Return the color scheme for this site"""
        return ("magenta", [])
    
    def getWebsiteURL(self) -> str:
        """Get the website URL for this streamer."""
        return f"https://www.chaturbate.com/{self.username}"
    
    def getVideoUrl(self) -> Optional[str]:
        """Get the video stream URL."""
        # If bulk_update is active, we need to fetch our own status for the URL
        if self.bulk_update:
            self.getStatus()
        # If lastInfo is missing or stale, try to refresh it
        if not self.lastInfo or 'url' not in self.lastInfo:
            self.logger.debug("lastInfo missing URL, refreshing status...")
            status = self.getStatus()
            if status != Status.PUBLIC:
                self.logger.warning(f"Cannot get video URL - status is {status}")
                return None
            if not self.lastInfo or 'url' not in self.lastInfo:
                self.logger.error("Still no URL after refresh")
                return None
            
        url = self.lastInfo['url']

        # Use CMAF if available for better streaming
        if self.lastInfo.get('cmaf_edge'):
            url = url.replace('playlist.m3u8', 'playlist_sfm4s.m3u8')
            url = re.sub(r'live-.+amlst', 'live-c-fhls/amlst', url)

        # Pass the playlist URL straight to ffmpeg instead of routing it through
        # getWantedResolutionPlaylist(). The CB ajax already returns the
        # wanted-quality stream (bandwidth=high), and re-routing it broke CB
        # entirely (0/36 recordings):
        #   - low-latency (llhls) MEDIA playlists have no variants, so
        #     getPlaylistVariants() returned [] -> "No available sources";
        #   - for master playlists it appended the master's short-lived token
        #     to the variant URL, which ffmpeg then hit as HTTP 403/404
        #     (decoded "Abnormal exit code" = AVERROR_HTTP_FORBIDDEN/NOT_FOUND).
        # ffmpeg handles both master and media playlists natively with the
        # fresh token. Verified live: direct ffmpeg capture of a CB URL records
        # cleanly (exit 0, ~1.8 MB in 8 s).

        # IMPORTANT: do NOT pre-probe / pre-fetch this URL. CB's edge token is
        # single-use / connection-bound -- ANY fetch of the tokenized playlist
        # (even a quick GET) consumes it, so ffmpeg, the next consumer, then gets
        # HTTP 403 Forbidden and EVERY capture fails with 0 bytes (verified: a
        # requests.get -> 200 immediately followed by ffmpeg on the same URL ->
        # 403). ffmpeg MUST be the first and only consumer. Stale-public / ghost
        # streams (online but no segments flowing) are handled downstream by
        # getVideoFfmpeg's no-data watchdog (aborts a capture writing 0 bytes
        # within NO_DATA_ABORT_SEC), not by a probe here.
        return url

    def getStatus(self) -> Status:
        """Check the current status of the stream."""
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": self.headers.get("User-Agent", "Mozilla/5.0")
        }
        data = {
            "room_slug": self.username, 
            "bandwidth": "high"
        }

        try:
            response = self.session.post(
                "https://chaturbate.com/get_edge_hls_url_ajax/",
                headers=headers,
                data=data,
                timeout=30,
                bucket='status'
            )
            
            if response.status_code != 200:
                self.logger.warning(f"HTTP {response.status_code} for user {self.username}")
                # Treat server errors as temporary (ratelimit) not permanent errors
                if response.status_code >= 500 or response.status_code == 429:
                    return Status.RATELIMIT
                elif response.status_code == 403:
                    # Cloudflare challenge / IP-reputation block ("Just a
                    # moment..."). Back off on the ratelimit cadence instead of
                    # fast ERROR retries, so the fleet stops hammering a flagged
                    # exit IP and making the block worse / longer.
                    return Status.RATELIMIT
                elif response.status_code == 404:
                    return Status.NOTEXIST
                return Status.ERROR
                
            self.lastInfo = response.json()

            # Defensive: if the API returns room_status: null (edge case),
            # .get('key','') returns None — not the default ''. Coerce via or ''.
            room_status = (self.lastInfo.get("room_status") or "").lower()
            status = self._parseStatus(room_status)
            if status == Status.PUBLIC and not self.lastInfo.get('url'):
                status = Status.RESTRICTED
                
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Network error checking status: {e}")
            status = Status.RATELIMIT
        except (KeyError, ValueError) as e:
            self.logger.error(f"Error parsing response: {e}")
            status = Status.ERROR
        except (TimeoutError, ConnectionError, OSError) as e:
            # curl_cffi (used by CFSessionManager) raises a parallel
            # exception tree that does NOT inherit from
            # requests.exceptions.RequestException — its TimeoutError /
            # ConnectionError / DNSError all subclass OSError. Without
            # this catch, every CB bot's status timeout fell through to
            # the catch-all below and got logged as a noisy ERROR plus
            # Status.ERROR (which doesn't trigger backoff).
            #
            # Treat as RATELIMIT so the bot's adaptive backoff increases
            # the polling delay — same effect as a 429/5xx response.
            # Demoted from ERROR to DEBUG since these are common
            # transient failures under load (3000+/day at 700-bot scale,
            # per the May 2026 streamonitor.log audit).
            self.logger.debug(f"Network/timeout {type(e).__name__}: {e}")
            status = Status.RATELIMIT
        except Exception as e:
            self.logger.error(f"Unexpected error [{type(e).__name__}]: {e!r}")
            status = Status.ERROR

        self.ratelimit = status == Status.RATELIMIT
        return status

    @staticmethod
    def _parseStatus(room_status: str) -> Status:
        """Parse room status string into Status enum."""
        if room_status == "public":
            return Status.PUBLIC
        elif room_status in ("private", "hidden"):
            return Status.PRIVATE
        elif room_status == "offline":
            return Status.OFFLINE
        else:
            return Status.OFFLINE

    @classmethod
    def getStatusBulk(cls, streamers: Set['Chaturbate']) -> None:
        """Bulk status update using the affiliates API."""
        session = requests.Session()
        session.headers.update(cls.headers)
        try:
            r = session.get("https://chaturbate.com/affiliates/api/onlinerooms/?format=json&wm=DkfRj", timeout=10)
            try:
                data = r.json()
            except requests.exceptions.JSONDecodeError:
                return

            data_map = {str(model['username']).lower(): model for model in data}

            for streamer in streamers:
                model_data = data_map.get(streamer.username.lower())
                if not model_data:
                    streamer.setStatus(Status.OFFLINE)
                    continue
                if model_data.get('gender'):
                    streamer.gender = cls._GENDER_MAP.get(model_data['gender'], Gender.UNKNOWN)
                if model_data.get('country'):
                    streamer.country = model_data.get('country', '').upper()
                status = cls._parseStatus(model_data.get('current_show', ''))
                # Trust the affiliates current_show for status. We deliberately
                # do NOT spend a per-model get_edge_hls_url_ajax to "confirm"
                # PUBLIC here: that ajax (Cloudflare-gated chaturbate.com) across
                # 600+ bots is what flags the VPN exit IP into a site-wide 403
                # ("Just a moment..."). The stream URL is fetched lazily at
                # recording start (getVideoUrl), so confirming here is pure
                # wasted Cloudflare exposure. Keep the skip for already-live bots
                # so we never disturb an in-progress recording.
                if status == Status.PUBLIC and streamer.sc in (Status.PUBLIC, Status.RESTRICTED):
                    continue
                streamer.setStatus(status)
        except Exception as e:
            # Silently fail for bulk — individual bots will still poll on their own
            pass

    def isMobile(self) -> bool:
        """Check if this is a mobile broadcast."""
        return False
