import requests
from typing import Optional, Dict, Any, Tuple, List
from streamonitor.bot import Bot
from streamonitor.enums import Status


class XLoveCam(Bot):
    site: str = 'XLoveCam'
    siteslug: str = 'XLC'

    def __init__(self, username: str) -> None:
        super().__init__(username)
        self._id: Optional[int] = self.getPerformerId()
        self.url = self.getWebsiteURL()

    def get_site_color(self) -> Tuple[str, List[str]]:
        """Return the color scheme for this site."""
        return ("white", [])

    def getWebsiteURL(self) -> str:
        """Get the website URL for this streamer."""
        return f"https://www.xlovecam.com/{self.username}"

    def getPerformerId(self) -> Optional[int]:
        """Get the performer ID for the username."""
        try:
            data = {
                'config[nickname]': self.username,
                'config[favorite]': "0",
                'config[recent]': "0",
                'config[vip]': "0",
                'config[sort][id]': "35",
                'offset[from]': "0",
                'offset[length]': "35",
                'origin': "filter-chg",
                'stat': "0",
            }
            
            response = self.session.post(
                'https://www.xlovecam.com/hu/performerAction/onlineList',
                headers=self.headers,
                data=data,
                timeout=30,
                bucket='auth'
            )
            
            if not response.ok:
                self.logger.warning(f"Failed to get performer list: HTTP {response.status_code}")
                return None
                
            resp = response.json()
            
            content = resp.get('content', {})
            performer_list = content.get('performerList', [])
            
            for performer in performer_list:
                if isinstance(performer, dict):
                    nickname = (performer.get('nickname') or '').lower()
                    if nickname == self.username.lower():
                        return performer.get('id')
            return None
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Network error getting performer ID: {e}")
            return None
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
            self.logger.error(f"Error parsing performer response: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error getting performer ID: {e}")
            return None

    def getVideoUrl(self) -> Optional[str]:
        """Get the video stream URL."""
        if not self.lastInfo:
            return None
        return self.lastInfo.get('hlsPlaylistFree')

    def getStatus(self) -> Status:
        """Check the current status of the stream."""
        if self._id is None:
            return Status.NOTEXIST

        try:
            data = {
                'performerId': self._id,
            }
            
            response = self.session.post(
                'https://www.xlovecam.com/hu/performerAction/getPerformerRoom',
                headers=self.headers,
                data=data,
                timeout=30,
                bucket='status'
            )

            if not response.ok:
                self.logger.warning(f"HTTP {response.status_code} for user {self.username}")
                return Status.UNKNOWN
                
            resp_data = response.json()
            
            if 'content' not in resp_data:
                return Status.UNKNOWN
            if 'performer' not in resp_data['content']:
                return Status.UNKNOWN
                
            self.lastInfo = resp_data['content']['performer']

            if not self.lastInfo.get('enabled'):
                return Status.NOTEXIST
                
            online_status = self.lastInfo.get('online')
            
            if online_status == 1:
                # Check if there's a free stream available
                if 'hlsPlaylistFree' not in self.lastInfo:
                    return Status.UNKNOWN
                # Upstream fix (lossless1024/StreaMonitor 5153beb): the
                # hlsPlaylistFree URL is still PRESENT while the model is in a
                # private show — it just serves a JSON error instead of a
                # playlist. Taking its presence as "public" made us hand ffmpeg
                # a non-playlist and log "Abnormal exit code" on repeat. Fetch
                # it and require real HLS before claiming PUBLIC.
                try:
                    r_m3u = self.session.get(self.lastInfo['hlsPlaylistFree'],
                                             headers=self.headers, timeout=15)
                    m3u_data = r_m3u.content or b''
                except (requests.exceptions.RequestException, TimeoutError,
                        ConnectionError, OSError) as e:
                    # Don't demote to PRIVATE on a network blip — that would
                    # make a live model look unavailable. Retry next poll.
                    self.logger.debug(f"Playlist probe failed: {type(e).__name__}: {e}")
                    return Status.UNKNOWN
                if m3u_data.startswith(b'{') or b'EXTM3U' not in m3u_data:
                    return Status.PRIVATE
                return Status.PUBLIC
            elif online_status == 0:
                return Status.OFFLINE
            else:
                return Status.UNKNOWN
                
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
