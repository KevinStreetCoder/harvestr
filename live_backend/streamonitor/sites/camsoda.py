import os
import re
import time
import threading
import requests
from typing import Optional, Dict, List, Any, Union
from urllib.parse import urljoin, urlparse, urlunparse, urlencode, parse_qsl
from streamonitor.bot import Bot
from streamonitor.enums import Status


def _getVideoCamSoda(self_bot, url: str, filename: str) -> bool:
    """
    CamSoda-specific HLS downloader that uses curl_cffi (via self.session) to
    fetch both the playlist and segments.  CamSoda's CDN rejects plain
    requests / ffmpeg based on TLS fingerprint, so we must use the bot's
    impersonated session for every HTTP call.

    Writes raw MPEG-TS segments sequentially into *filename* (already ends
    with the correct extension set by genOutFilename).
    """
    sess = self_bot.session          # curl_cffi CFSessionManager
    headers = dict(self_bot.headers or {})
    stop_flag = threading.Event()

    def _stop():
        stop_flag.set()

    self_bot.stopDownload = _stop

    # ── Resolve the base URL for relative segment URIs ──
    parsed = urlparse(url)
    base_dir = parsed._replace(
        path=parsed.path.rsplit("/", 1)[0] + "/",
        query="", fragment=""
    )
    base_url_str = urlunparse(base_dir)
    parent_qs = dict(parse_qsl(parsed.query or ""))

    def abs_url(maybe_rel: str) -> str:
        """Make URL absolute and inherit parent query parameters."""
        if maybe_rel.startswith(("http://", "https://")):
            absu = maybe_rel
        else:
            absu = urljoin(base_url_str, maybe_rel)
        up = urlparse(absu)
        q_child = dict(parse_qsl(up.query or ""))
        merged = dict(parent_qs)
        merged.update(q_child)
        return urlunparse(up._replace(query=urlencode(merged)))

    # ── ensure output dir exists ──
    out_dir = os.path.dirname(filename)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # Change extension to .tmp.ts for compatibility with the rest of the system
    base_name = os.path.splitext(filename)[0]
    output_path = base_name + ".tmp.ts"

    seen_segs: set = set()
    total_bytes = 0
    consecutive_errors = 0
    stall_since = time.monotonic()
    MAX_STALL = 45          # seconds with no new data → give up
    POLL_INTERVAL = 2.0     # how often to poll the media playlist
    init_fetched = False
    init_url_last: Optional[str] = None

    try:
        with open(output_path, "wb") as fp:
            while not stop_flag.is_set():
                # ── fetch media playlist ──
                try:
                    r = sess.get(url, headers=headers, timeout=10)
                    if r.status_code != 200:
                        consecutive_errors += 1
                        # 403 = token expired, 500 = stream ended on CDN
                        if r.status_code in (403, 500):
                            if consecutive_errors >= 3:
                                self_bot.logger.warning(
                                    f"Persistent HTTP {r.status_code} — stream likely ended, stopping capture"
                                )
                                break
                        else:
                            self_bot.logger.warning(f"Playlist HTTP {r.status_code}")
                        time.sleep(POLL_INTERVAL)
                        continue
                    playlist_text = r.content.decode("utf-8", errors="ignore")
                    consecutive_errors = 0
                except Exception as e:
                    self_bot.logger.warning(f"Playlist fetch error: {e}")
                    time.sleep(POLL_INTERVAL)
                    continue

                # ── parse EXT-X-MAP (init segment) ──
                map_match = re.search(r'#EXT-X-MAP:URI="([^"]+)"', playlist_text)
                if map_match:
                    map_uri = abs_url(map_match.group(1))
                    if map_uri != init_url_last:
                        # New init segment – must write it
                        try:
                            ri = sess.get(map_uri, headers=headers, timeout=10)
                            if ri.status_code == 200 and ri.content:
                                fp.write(ri.content)
                                fp.flush()
                                total_bytes += len(ri.content)
                                stall_since = time.monotonic()
                                init_url_last = map_uri
                                init_fetched = True
                        except Exception:
                            pass

                # ── collect segment URIs (skip PART lines for simplicity) ──
                new_segs: list = []
                for line in playlist_text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    seg_url = abs_url(line)
                    if seg_url not in seen_segs:
                        new_segs.append(seg_url)
                        seen_segs.add(seg_url)

                # ── download new segments ──
                for seg_url in new_segs:
                    if stop_flag.is_set():
                        break
                    try:
                        rs = sess.get(seg_url, headers=headers, timeout=15)
                        if rs.status_code == 200 and rs.content:
                            fp.write(rs.content)
                            fp.flush()
                            total_bytes += len(rs.content)
                            stall_since = time.monotonic()
                    except Exception as e:
                        self_bot.logger.debug(f"Segment error: {e}")

                # ── stall detection ──
                if time.monotonic() - stall_since > MAX_STALL:
                    self_bot.logger.warning("Stream stalled – ending capture")
                    break

                # ── endlist? ──
                if "#EXT-X-ENDLIST" in playlist_text:
                    self_bot.logger.info("Playlist ended (ENDLIST)")
                    break

                time.sleep(POLL_INTERVAL)

    except Exception as e:
        self_bot.logger.error(f"CamSoda download error: {e}")
        return False

    # ── validate output ──
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        size = os.path.getsize(output_path)
        self_bot.logger.info(f"Captured {size / 1024 / 1024:.1f} MB")
        return True

    self_bot.logger.error("No data captured")
    return False


class CamSoda(Bot):
    site: str = 'CamSoda'
    siteslug: str = 'CS'
    API_BASE: str = "https://www.camsoda.com/api/v1/chat/react"

    bulk_update: bool = True  # Use bulk status manager instead of per-bot polling

    def __init__(self, username: str) -> None:
        super().__init__(username)
        self.vr = False  # CamSoda doesn't have VR
        self.url = self.getWebsiteURL()
        self.lastInfo: Dict[str, Any] = {}
        # Use custom CamSoda downloader that uses curl_cffi for all HTTP calls.
        # CamSoda's CDN blocks ffmpeg and plain requests via TLS fingerprinting.
        self.getVideo = lambda _, url, filename: _getVideoCamSoda(self, url, filename)

    def get_site_color(self):
        """Return the color scheme for this site"""
        return ("blue", [])

    # ──────────────── Core API ────────────────
    @classmethod
    def getStatusBulk(cls, streamers):
        """
        Bulk status check: polls each CamSoda streamer sequentially with pacing
        to avoid 429 rate limits. Called by BulkStatusManager every ~10s.
        Individual bots don't call getStatus() when bulk_update=True.
        """
        # Use the first streamer's session for API calls (curl_cffi with TLS impersonation)
        if not streamers:
            return

        # Pick any running streamer's session
        sess = None
        for s in streamers:
            if hasattr(s, 'session') and s.session:
                sess = s.session
                break
        if sess is None:
            return

        headers = cls.headers
        consecutive_429 = 0

        for streamer in streamers:
            if not streamer.running:
                continue

            # If we're getting hammered with 429s, back off harder
            if consecutive_429 >= 3:
                time.sleep(5.0)
                consecutive_429 = 0

            try:
                r = sess.get(
                    f"{cls.API_BASE}/{streamer.username}",
                    headers=headers,
                    timeout=15,
                    bucket='status'
                )

                if r.status_code in (403, 429):
                    consecutive_429 += 1
                    streamer.setStatus(Status.RATELIMIT)
                    time.sleep(2.0)  # Extra delay on rate limit
                    continue

                consecutive_429 = 0

                if r.status_code != 200:
                    streamer.setStatus(Status.UNKNOWN)
                    time.sleep(0.5)
                    continue

                data = r.json()
                streamer.lastInfo = data

                if "error" in data and data["error"] == "No username found.":
                    streamer.setStatus(Status.NOTEXIST)
                elif "stream" not in data:
                    streamer.setStatus(Status.UNKNOWN)
                else:
                    stream_data = data["stream"]
                    if "edge_servers" in stream_data and len(stream_data["edge_servers"]) > 0:
                        streamer.setStatus(Status.PUBLIC)
                    elif "private_servers" in stream_data and len(stream_data["private_servers"]) > 0:
                        streamer.setStatus(Status.PRIVATE)
                    elif "token" in stream_data:
                        streamer.setStatus(Status.OFFLINE)
                    else:
                        streamer.setStatus(Status.UNKNOWN)

            except Exception as e:
                streamer.setStatus(Status.ERROR)

            # Pace requests: ~1 req/sec to stay under CamSoda's rate limit
            time.sleep(1.0)

    def fetchInfo(self) -> Dict[str, Any]:
        """Fetch raw JSON info for the performer."""
        try:
            response = self.session.get(
                f"{self.API_BASE}/{self.username}",
                headers=self.headers,
                timeout=30,
                bucket='status'
            )

            if response.status_code in (403, 429):
                return {"__status__": Status.RATELIMIT}
            elif response.status_code != 200:
                self.logger.warning(f"HTTP {response.status_code} for user {self.username}")
                return {"__status__": Status.UNKNOWN}

            data = response.json()
            self.lastInfo = data
            return data

        except requests.exceptions.RequestException as e:
            self.logger.error(f"Network error fetching info: {e}")
            return {"__status__": Status.ERROR}
        except (KeyError, ValueError) as e:
            self.logger.error(f"Error parsing response: {e}")
            return {"__status__": Status.ERROR}
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}")
            return {"__status__": Status.ERROR}

    # ──────────────── Convenience methods ────────────────
    def getWebsiteURL(self) -> str:
        """Get the website URL for this streamer."""
        return f"https://www.camsoda.com/{self.username}"

    # ──────────────── Status evaluation ────────────────
    def getStatus(self) -> Status:
        """Check the current status of the stream."""
        data = self.fetchInfo()
        if "__status__" in data:
            return data["__status__"]

        if "error" in data and data["error"] == "No username found.":
            return Status.NOTEXIST

        if "stream" not in data:
            return Status.UNKNOWN

        stream_data = data["stream"]
        if "edge_servers" in stream_data and len(stream_data["edge_servers"]) > 0:
            return Status.PUBLIC
        if "private_servers" in stream_data and len(stream_data["private_servers"]) > 0:
            return Status.PRIVATE
        if "token" in stream_data:
            return Status.OFFLINE
        return Status.UNKNOWN

    # ──────────────── Video URL ────────────────
    def getVideoUrl(self) -> Optional[str]:
        """Get the video stream URL."""
        if not self.lastInfo:
            return None

        stream = self.lastInfo.get("stream", {})
        servers = stream.get("edge_servers", [])
        stream_name = stream.get("stream_name")
        token = stream.get("token")

        if not servers or not stream_name or not token:
            return None

        base = servers[0]
        if not base.startswith("http"):
            base = "https://" + base

        track_params = "filter=tracks:v4v3v2v1a1a2&multitrack=true"
        master_url = f"{base}/{stream_name}_v1/index.ll.m3u8?{track_params}&token={token}"

        # Let the shared resolution selector handle variant picking
        return self.getWantedResolutionPlaylist(master_url)

    def isMobile(self) -> bool:
        """Check if this is a mobile broadcast."""
        return False
