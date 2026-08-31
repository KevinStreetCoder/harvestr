import re
import time
import requests
import base64
import hashlib
import random
import itertools
import json
import os
from functools import lru_cache
from typing import Optional, Tuple, List, Dict

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from streamonitor.bot import RoomIdBot
from streamonitor.downloaders.hls import getVideoNativeHLS
from streamonitor.enums import Status, Gender
from streamonitor.utils.CloudflareDetection import looks_like_cf_html


class StripChat(RoomIdBot):
    site = 'StripChat'
    siteslug = 'SC'
    bulk_update = True

    _GENDER_MAP = {
        'female': Gender.FEMALE,
        'male': Gender.MALE,
        'couple': Gender.BOTH,
        'trans': Gender.TRANS,
    }

    _static_data = None
    _main_js_data = None
    _doppio_js_data = None
    _mouflon_keys: dict = {
        "Zeechoej4aleeshi": "ubahjae7goPoodi6",
        "Zokee2OhPh9kugh4": "Quean4cai9boJa5a",
        "Ook7quaiNgiyuhai": "EQueeGh2kaewa3ch",
    }
    _session = None

    # Pre-compiled regex patterns
    _DOPPIO_INDEX_PATTERN = re.compile(r'([0-9]+):"Doppio"')
    _DOPPIO_REQUIRE_PATTERN = re.compile(r'require\(["\']\./(Doppio[^"\']+\.js)["\']\)')
    # New webpack chunk pattern: looks for DoppioWrapper being loaded from chunk
    # Pattern: Promise.all([n.e(149),n.e(184)]).then(n.bind(n,4184))).DoppioWrapper
    _DOPPIO_CHUNK_PATTERN = re.compile(r'n\.e\((\d+)\)\]\)\.then\(n\.bind\(n,\d+\)\)\)\.DoppioWrapper')
    # Chunk hash mapping pattern: n.u=e=>"chunk-"+{149:"hash",184:"hash",...}[e]+".js"
    _CHUNK_HASH_PATTERN = re.compile(r'n\.u=e=>"chunk-"\+\{([^}]+)\}\[e\]\+"\.js"')
    
    # Constants
    _MOUFLON_NEEDLE = "#EXT-X-MOUFLON:"
    _MOUFLON_FILE_ATTR = "#EXT-X-MOUFLON:FILE:"
    _MOUFLON_URI_ATTR = "#EXT-X-MOUFLON:URI:"
    _MOUFLON_FILENAME = "media.mp4"
    _CDN_DOMAINS = ("org", "com", "net")
    _CHARSET = "abcdefghijklmnopqrstuvwxyz0123456789"
    
    # Status sets for O(1) lookup
    # groupShow included in private - requires ticket to view
    _PRIVATE_STATUSES = frozenset(["private", "groupShow", "p2p", "virtualPrivate", "p2pVoice"])
    _OFFLINE_STATUSES = frozenset(["off", "idle"])

    def __init__(self, username, room_id=None):
        if StripChat._static_data is None:
            StripChat._static_data = {}
            try:
                StripChat.getInitialData()
            except Exception as e:
                StripChat._static_data = None
                raise e
        
        # Load mouflon keys from cache file if available
        StripChat._loadMouflonKeysFromCache()
        
        # Fast wait with timeout
        end_time = time.time() + 15
        while StripChat._static_data == {} and time.time() < end_time:
            time.sleep(0.05)
        
        if StripChat._static_data == {}:
            raise TimeoutError("Static data initialization timeout")
        
        super().__init__(username, room_id=room_id)
        self.vr = False
        self.getVideo = lambda _, url, filename: getVideoNativeHLS(self, url, filename, StripChat.m3u_decoder)
        # doppiocdn stream tokens are bound to the exit IP, so a VPN rotation kills
        # the in-flight token -- the capture can't ride through; the LiveManager
        # restarts it on rotation to grab a fresh token (see tokens_ip_bound).
        self.tokens_ip_bound = True

    def get_site_color(self):
        """Return the color scheme for this site"""
        return ("green", [])

    @classmethod
    def _get_session(cls):
        """Optimized session with connection pooling"""
        if cls._session is None:
            cls._session = requests.Session()
            
            retry = Retry(
                total=2,
                backoff_factor=0.1,
                status_forcelist=[429, 500, 502, 503, 504],
            )
            
            adapter = HTTPAdapter(
                max_retries=retry,
                pool_connections=15,
                pool_maxsize=30,
                pool_block=False
            )
            
            cls._session.mount("http://", adapter)
            cls._session.mount("https://", adapter)
            cls._session.headers.update({
                'Connection': 'keep-alive',
                'Accept-Encoding': 'gzip, deflate',
            })
            
            # Proxy support
            if http_proxy := os.getenv('HTTP_PROXY'):
                cls._session.proxies['http'] = http_proxy
            if https_proxy := os.getenv('HTTPS_PROXY'):
                cls._session.proxies['https'] = https_proxy
        
        return cls._session

    @classmethod
    def getInitialData(cls):
        """Fetch static configuration and parse mouflon keys from Doppio JS."""
        s = cls._get_session()

        try:
            r = s.get(
                "https://hu.stripchat.com/api/front/v3/config/static",
                headers=cls.headers,
                timeout=5
            )
            r.raise_for_status()
            response_json = r.json()

            # Handle different response structures
            if "static" in response_json:
                static_data = response_json["static"]
            else:
                static_data = response_json

            # The API structure has changed - use featureSettings instead of features
            if "featureSettings" in static_data:
                feature_settings = static_data["featureSettings"]
                mmp_origin = feature_settings.get("MMPExternalUnitedSourceOrigin", feature_settings["MMPExternalSourceOrigin"])
            elif "features" in static_data:
                # Fallback to old API structure
                feature_settings = static_data["features"]
                mmp_origin = feature_settings.get("MMPExternalUnitedSourceOrigin", feature_settings["MMPExternalSourceOrigin"])
            else:
                raise Exception(f"'featureSettings' not found. Available keys: {list(static_data.keys())}")

            # New StripChat config schema (May 2026) removed `featuresV2`
            # entirely. The MMP version is no longer in the API config; it's
            # now embedded in the homepage HTML as part of the player URL.
            # Adopting the upstream fix from lossless1024/StreaMonitor#268:
            #
            #   1. Scrape the homepage HTML for the regex
            #        mmp\.doppiocdn\.com/player/mmp/(v[\d.]+)/
            #   2. Fall back to a known-good hardcoded version if scrape fails
            #
            # If both fail (no network, blocked, etc.) we soft-fail — populate
            # static_data with cached keys so add_model() succeeds and status
            # polling continues. Only the on-the-fly mouflon key refresh is
            # then disabled, but cached keys carry recording.
            _MMP_FALLBACK_VERSION = "v2.6.0"
            mmp_version = _MMP_FALLBACK_VERSION
            if "featuresV2" in static_data and \
                    "playerModuleExternalLoading" in static_data["featuresV2"]:
                # Old schema still around (rare) — use it
                mmp_version = static_data["featuresV2"]["playerModuleExternalLoading"]["mmpVersion"]
            else:
                # Scrape the version from the homepage
                try:
                    r_home = s.get("https://stripchat.com", headers=cls.headers,
                                    timeout=5)
                    r_home.raise_for_status()
                    version_match = re.search(
                        r'mmp\.doppiocdn\.com/player/mmp/(v[\d.]+)/',
                        r_home.text
                    )
                    if version_match:
                        mmp_version = version_match.group(1)
                except Exception:
                    # Couldn't reach homepage — fall back to hardcoded version
                    pass

            mmp_base = f"{mmp_origin}/{mmp_version}"

            # Fetch main.js
            r = s.get(f"{mmp_base}/main.js", headers=cls.headers, timeout=5)
            r.raise_for_status()
            main_js_data = r.text

            # Find Doppio JS file
            doppio_js_name = None

            # Try direct require pattern first (legacy)
            if match := cls._DOPPIO_REQUIRE_PATTERN.search(main_js_data):
                doppio_js_name = match[1]
            # Try new webpack chunk pattern: n.e(184)...DoppioWrapper
            elif match := cls._DOPPIO_CHUNK_PATTERN.search(main_js_data):
                chunk_id = match[1]
                # Find the chunk hash mapping
                if hash_match := cls._CHUNK_HASH_PATTERN.search(main_js_data):
                    chunk_mapping = hash_match[1]
                    # Parse the mapping to find the hash for our chunk_id
                    # Format: 149:"hash1",184:"hash2",...
                    for mapping in chunk_mapping.split(','):
                        if ':' in mapping:
                            cid, chash = mapping.split(':', 1)
                            if cid.strip() == chunk_id:
                                # Remove quotes from hash
                                chash = chash.strip().strip('"')
                                doppio_js_name = f"chunk-{chash}.js"
                                break
            elif match := cls._DOPPIO_INDEX_PATTERN.search(main_js_data):
                idx = match[1]
                # Look for hash in various formats
                for pattern in [
                    rf'{idx}:\\"([a-zA-Z0-9]{{20}})\\"',
                    rf'{idx}:"([a-zA-Z0-9]{{20}})"',
                    rf'"{idx}":"([a-zA-Z0-9]{{20}})"',
                ]:
                    if hash_match := re.search(pattern, main_js_data):
                        doppio_js_name = f"chunk-Doppio-{hash_match[1]}.js"
                        break

            if not doppio_js_name:
                raise Exception("Could not find Doppio JS file in main.js")

            r = s.get(f"{mmp_base}/{doppio_js_name}", headers=cls.headers, timeout=5)
            r.raise_for_status()
            doppio_js_data = r.text

            # Only update class variables after everything succeeds
            StripChat._static_data = static_data
            StripChat._main_js_data = main_js_data
            StripChat._doppio_js_data = doppio_js_data

        except Exception as e:
            print(f"ERROR in getInitialData: {e}")
            raise

    @staticmethod
    def uniq(length: int = 16) -> str:
        return ''.join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=length))

    @classmethod
    @lru_cache(maxsize=512)
    def _get_hash_bytes(cls, key: str) -> bytes:
        return hashlib.sha256(key.encode()).digest()

    @classmethod
    def _parseMouflonKeys(cls):
        """
        Extract mouflon pkey/pdkey from Doppio JS dynamically.
        
        The keys are built from a specific formula in the obfuscated JS:
        - Base36 conversions of fixed numbers
        - Character shifts
        - IIFEs that decode numeric arguments
        
        Current known formula (v2.0.10):
        pkey = "Zeechoej4aleeshi"
        pdkey = "ubahjae7goPoodi6"
        """
        js_data = cls._doppio_js_data
        if not js_data:
            return
        
        try:
            # Try to extract keys from the ns expression
            keys = cls._extractNsKeys(js_data)
            if keys:
                cls._mouflon_keys[keys[0]] = keys[1]
                return
            
            # Fallback to legacy patterns
            cls._parseLegacyMouflonKeys()
            
        except Exception as e:
            print(f"[StripChat] Warning: Failed to parse mouflon keys: {e}")
            cls._parseLegacyMouflonKeys()

    @classmethod
    def _extractNsKeys(cls, js_data):
        """
        Extract pkey/pdkey from the Jn/ss/ns expression in Doppio JS.
        
        v2.1.3 key construction (const Jn=):
        - pkey: IIFE(45,196,195,...) + 36918.toString(36) = "Zeechoej4aleeshi"
        - pdkey: 0xaf004b1e62348.toString(36) + shifted(32) + 24.toString(36) + IIFE_first4 = "ubahjae7goPoodi6"
        
        The keys are built from:
        1. Fixed numbers converted to base36
        2. Character shifts (-39)
        3. IIFE patterns with specific offsets
        """
        
        def to_base36(n):
            chars = '0123456789abcdefghijklmnopqrstuvwxyz'
            if n == 0:
                return '0'
            result = ''
            while n > 0:
                result = chars[n % 36] + result
                n //= 36
            return result
        
        def shift_chars(s, offset):
            return ''.join(chr(ord(c) + offset) for c in s)
        
        def decode_iife_v213(args, offset=38):
            """Decode v2.1.3 IIFE: first arg is offset, reverse remaining, then (a - first - offset) - i"""
            first = args[0]
            remaining = args[1:][::-1]
            return ''.join(chr((a - first - offset) - i) for i, a in enumerate(remaining))
        
        def decode_iife_v213_pdkey(args, offset=39):
            """Decode v2.1.3 pdkey IIFE: (a - first - offset) - i"""
            first = args[0]
            remaining = args[1:][::-1]
            return ''.join(chr((a - first - offset) - i) for i, a in enumerate(remaining))
        
        # Check for v2.1.3 'const Jn=' pattern
        if 'const Jn=' in js_data:
            try:
                start = js_data.find('const Jn=')
                if start != -1:
                    chunk = js_data[start:start+3000]
                    
                    # Find all IIFEs in the chunk - they appear as }(num,num,num,...)
                    all_iifes = re.findall(r'\}\((\d+(?:,\d+)+)\)', chunk)
                    iifes = []
                    for iife_str in all_iifes:
                        args = [int(x) for x in iife_str.split(',')]
                        iifes.append(args)
                    
                    # First IIFE (14 args starting with ~45) is for pkey
                    pkey_part1 = ''
                    for args in iifes:
                        if len(args) >= 10 and len(args) <= 16 and 40 <= args[0] <= 50:
                            pkey_part1 = decode_iife_v213(args, 38)
                            break
                    
                    # Find 36918.toString(36) for "shi"
                    if '36918' in chunk:
                        pkey_part2 = to_base36(36918)
                    else:
                        pkey_part2 = ''
                    
                    # Find the large hex number for pdkey: 0xaf004b1e62348 or decimal equivalent
                    pdkey_part1 = ''
                    hex_match = re.search(r'0x([0-9a-fA-F]+)', chunk)
                    if hex_match:
                        hex_val = int(hex_match.group(1), 16)
                        pdkey_part1 = to_base36(hex_val)
                    else:
                        # Try decimal: look for large number > 100000000000
                        for m in re.finditer(r'\b(\d{12,16})\b', chunk):
                            n = int(m.group(1))
                            if n > 100000000000:
                                pdkey_part1 = to_base36(n)
                                break
                    
                    # 32 shifted by -39 for 'P'
                    pdkey_part2 = shift_chars(to_base36(32), -39) if '32' in chunk else ''
                    
                    # 24.toString(36) for 'o'
                    pdkey_part3 = to_base36(24) if '24' in chunk else ''
                    
                    # Second IIFE (19-20 args starting with ~42) is for pdkey 'odi6' part
                    pdkey_part4 = ''
                    for args in iifes:
                        if len(args) >= 18 and len(args) <= 22 and 40 <= args[0] <= 45:
                            pdkey_part4 = decode_iife_v213_pdkey(args, 39)[:4]  # Only first 4 chars
                            break
                    
                    pkey = pkey_part1 + pkey_part2
                    pdkey = pdkey_part1 + pdkey_part2 + pdkey_part3 + pdkey_part4
                    
                    # Both keys should be 16 characters
                    if len(pkey) == 16 and len(pdkey) == 16:
                        print(f"[StripChat] Extracted v2.1.3 keys: pkey={pkey}, pdkey={pdkey}")
                        return (pkey, pdkey)
                    elif len(pkey) >= 12 and len(pdkey) >= 12:
                        print(f"[StripChat] Partially extracted keys: pkey={pkey}({len(pkey)}), pdkey={pdkey}({len(pdkey)})")
                        return (pkey, pdkey)
                        
            except Exception as e:
                print(f"[StripChat] v2.1.3 key extraction failed: {e}")
        
        # Helper for older patterns
        def decode_iife(args, offset):
            """Decode IIFE: reverse args[1:], then (a - args[0] - offset) - i"""
            first = args[0]
            remaining = args[1:][::-1]
            return ''.join(chr((a - first - offset) - i) for i, a in enumerate(remaining))
        
        # Check for the 'const ss=' pattern (v2.1.1+)
        if 'const ss=' in js_data:
            try:
                # Find the ss= area
                start = js_data.find('const ss=(')
                if start == -1:
                    start = js_data.find('const ss=')
                
                if start != -1:
                    chunk = js_data[start:start+10000]
                    
                    # Extract all numbers in toString(36) calls
                    # Format 1: 16..toString(36)
                    # Format 2: 1128328536208[Jt(0,0,0,561)](36)
                    numbers = {}
                    for m in re.finditer(r'(\d+)\.\.toString\(36\)', chunk):
                        numbers[int(m.group(1))] = to_base36(int(m.group(1)))
                    for m in re.finditer(r'(\d+)\[[A-Za-z]+\([^)]+\)\]\(36\)', chunk):
                        numbers[int(m.group(1))] = to_base36(int(m.group(1)))
                    
                    # Find IIFE patterns: }(num,num,num,...)
                    iifes = []
                    for m in re.finditer(r'\}\((\d+(?:,\d+)+)\)', chunk[:5000]):
                        args = [int(x) for x in m.group(1).split(',')]
                        if 2 <= len(args) <= 15:  # Reasonable IIFE size
                            iifes.append(args)
                    
                    # Build pkey using known pattern
                    # 16 >> -13 = 'Z'
                    p1 = shift_chars(to_base36(16), -13) if 16 in numbers else ''
                    
                    # Large number for 'eechoej4'
                    p2 = ''
                    for n in numbers:
                        if n > 1000000000000:  # Large number
                            p2 = numbers[n]
                            break
                    
                    # IIFE for 'ale' (offset 11, 3-4 args)
                    p3 = ''
                    for args in iifes:
                        if len(args) == 4 and 30 <= args[0] <= 40:
                            decoded = decode_iife(args, 11)
                            if decoded.isalpha() and decoded.islower():
                                p3 = decoded
                                break
                    
                    # 690102 = 'eshi'
                    p4 = numbers.get(690102, '')
                    
                    # Build pdkey
                    # 39286 = 'uba'
                    p5 = numbers.get(39286, '')
                    
                    # IIFE for 'hjae' (offset 10, 5 args)
                    p6 = ''
                    for args in iifes:
                        if len(args) == 5 and 60 <= args[0] <= 65:
                            decoded = decode_iife(args, 10)
                            if decoded.isalpha() and decoded.islower():
                                p6 = decoded
                                break
                    
                    # 9672 = '7go'
                    p7 = numbers.get(9672, '')
                    
                    # 32 >> -39 = 'P'
                    p8 = shift_chars(to_base36(32), -39) if 32 in numbers else ''
                    
                    # 888 = 'oo'
                    p9 = numbers.get(888, '')
                    
                    # IIFE for 'di' (offset 39, 3 args)
                    p10 = ''
                    for args in iifes:
                        if len(args) == 3 and 40 <= args[0] <= 50:
                            decoded = decode_iife(args, 39)
                            if decoded.isalpha() and decoded.islower():
                                p10 = decoded
                                break
                    
                    # 6 = '6'
                    p11 = numbers.get(6, '')
                    
                    pkey = p1 + p2 + p3 + p4
                    pdkey = p5 + p6 + p7 + p8 + p9 + p10 + p11
                    
                    if len(pkey) >= 12 and len(pdkey) >= 12:
                        print(f"[StripChat] Extracted keys: pkey={pkey}, pdkey={pdkey}")
                        return (pkey, pdkey)
                    
            except Exception as e:
                print(f"[StripChat] v2.1.1 key extraction failed: {e}")
        
        # Try legacy 'const ns=' pattern
        if 'const ns=' in js_data:
            try:
                # Find the two IIFEs with their arguments
                iife1_match = re.search(r'\}\((\d+(?:,\d+){8,12})\)', js_data)
                iife2_match = re.search(r'\}\((\d{2},\d{3},\d{3})\)', js_data)
                
                if iife1_match and iife2_match:
                    args1 = [int(x) for x in iife1_match.group(1).split(',')]
                    n1 = args1[0]
                    remaining1 = args1[1:][::-1]
                    p4 = ''.join(chr((a - n1 - 26) - i) for i, a in enumerate(remaining1))
                    
                    args2 = [int(x) for x in iife2_match.group(1).split(',')]
                    o2 = args2[0]
                    remaining2 = args2[1:][::-1]
                    p8 = ''.join(chr(((a - o2) - 56) - i) for i, a in enumerate(remaining2))
                    
                    p1 = ''.join(chr(ord(c) - 13) for c in to_base36(16))
                    p2 = to_base36(0x531f77594da7d).lower()
                    p3 = to_base36(18676).lower()
                    p5 = to_base36(662856).lower()
                    p6 = ''.join(chr(ord(c) - 39) for c in to_base36(32).lower())
                    p7 = to_base36(31981).lower()
                    
                    key_string = p1 + p2 + p3 + p4 + p5 + p6 + p7 + p8
                    
                    if ':' in key_string:
                        pkey, pdkey = key_string.split(':', 1)
                        if len(pkey) >= 8 and len(pdkey) >= 8:
                            return (pkey, pdkey)
            except Exception as e:
                print(f"[StripChat] Legacy key extraction failed: {e}")
        
        return None

    @classmethod
    def _parseLegacyMouflonKeys(cls):
        """Fallback: Parse legacy format 'pkey:pdkey' from Doppio JS."""
        if cls._doppio_js_data:
            legacy_pattern = r'"(\w{8,24}):(\w{8,24})"'
            matches = re.findall(legacy_pattern, cls._doppio_js_data)
            for pkey, pdkey in matches:
                cls._mouflon_keys[pkey] = pdkey

    @classmethod
    def _loadMouflonKeysFromCache(cls):
        """Load mouflon keys from stripchat_mouflon_keys.json cache file."""
        cache_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'stripchat_mouflon_keys.json')
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r') as f:
                    cached = json.load(f)
                if isinstance(cached, dict):
                    for k, v in cached.items():
                        if k not in cls._mouflon_keys:
                            cls._mouflon_keys[k] = v
            except Exception:
                pass

    @classmethod
    def _saveMouflonKeysToCache(cls):
        """Save mouflon keys to stripchat_mouflon_keys.json cache file."""
        cache_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'stripchat_mouflon_keys.json')
        try:
            with open(cache_file, 'w') as f:
                json.dump(cls._mouflon_keys, f, indent=2)
        except Exception:
            pass

    def getUsernameFromRoomId(self, room_id):
        """Resolve username from StripChat room/user ID."""
        try:
            r = self.session.get(
                f'https://stripchat.com/api/front/v2/models/{room_id}',
                headers=self.headers, bucket='api'
            )
            if r.status_code == 200:
                data = r.json()
                info = self.normalizeInfo(data)
                username = info.get('username')
                if username:
                    self.username = username
                    return username
        except Exception:
            pass
        return None

    # username -> numeric model id, resolved once and shared by every bot.
    # 659 SC bots polling on a loop must not each re-resolve on every cycle.
    _model_ids: Dict[str, str] = {}
    _model_ids_lock = None

    @classmethod
    def _resolveModelId(cls, session, username, headers, logger=None):
        """Resolve a StripChat username to its numeric model id.

        StripChat retired the username-keyed cam endpoint (upstream issues
        #368/#370): it now answers HTTP 418 with an empty body for every
        streamer. The replacement is two-step --
            /api/front/users/user-ids/{username}  -> {"id": N}
            /api/front/v2/models/{N}/cam          -> {"cam": ..., "user": ...}
        Same response shape as before, so only the lookup changes.
        """
        import threading
        if cls._model_ids_lock is None:
            cls._model_ids_lock = threading.Lock()
        key = username.lower()
        with cls._model_ids_lock:
            hit = cls._model_ids.get(key)
        if hit:
            return hit
        try:
            r = session.get(
                f'https://stripchat.com/api/front/users/user-ids/{username}',
                headers=headers, bucket='api')
            if r.status_code == 404:
                return None
            if r.status_code != 200:
                if logger:
                    logger.debug(f'user-ids lookup HTTP {r.status_code}')
                return None
            mid = (r.json() or {}).get('id')
        except Exception as e:
            if logger:
                logger.debug(f'user-ids lookup failed: {type(e).__name__}: {e}')
            return None
        if not mid:
            return None
        mid = str(mid)
        with cls._model_ids_lock:
            cls._model_ids[key] = mid
        return mid

    def getRoomIdFromUsername(self, username):
        """Resolve room/user ID from username."""
        try:
            mid = self._resolveModelId(self.session, username, self.headers,
                                       self.logger)
            if not mid:
                return None
            r = self.session.get(
                f'https://stripchat.com/api/front/v2/models/{mid}/cam',
                headers=self.headers, bucket='api'
            )
            if r.status_code == 200:
                data = r.json()
                info = self.normalizeInfo(data)
                # Try common paths for the user/model ID
                for path in [['user', 'id'], ['user', 'user', 'id'], ['model', 'id'], ['id']]:
                    val = self._get_by_path(info, path) if hasattr(self, '_get_by_path') else None
                    if val is None:
                        cur = info
                        for p in path:
                            if isinstance(cur, dict) and p in cur:
                                cur = cur[p]
                            else:
                                cur = None
                                break
                        val = cur
                    if val is not None:
                        return str(val)
        except Exception:
            pass
        return None

    @classmethod
    def getStatusBulk(cls, streamers):
        """Bulk status check for all StripChat streamers.

        StripChat's old bulk endpoint
        (`/api/front/v2/models?limit=500&parentCategories=...`) was
        retired in early 2026 — it now returns HTTP 400 with
        `{"error":"invalid 'primaryTag' value"}`. The new
        `?primaryTag=girls&limit=10` returns a "blocks" homepage layout
        rather than a flat model list, with no per-username status
        info.

        Verified working endpoints (May 2026):
          - `/api/front/users/user-ids/{u}` -> {"id": N}, then
            `/api/front/v2/models/{N}/cam?uniq=...` — the per-bot status
            endpoint. RETIRED (Aug 2026): the old username-keyed form
            `/api/front/v2/models/username/{u}/cam` now answers HTTP 418
            with an empty body for every streamer (upstream #368/#370).

        So we fall back to PARALLEL per-username polling for the
        bulk method. Concurrency is bounded by the shared HTTP semaphore
        in `streamonitor/utils/cf_session.py` (default 32) so we don't
        socket-exhaust the host even with 700+ models.

        Symptom this fixes: prior to this change, bulk silently
        returned without setting any status (line `if r.status_code
        != 200: return`). The bot's main loop then SKIPPED individual
        `getStatus()` calls because `bulk_update=True` and
        `sc != NOTRUNNING`. Effect: SC bots got their initial status
        on first poll, then NEVER updated again — meaning we missed
        every public-window opening after startup. In a 3-day log
        window, this resulted in only 27 download attempts across
        700+ models (vs. 15689 for Streamate where bulk works).
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        # Per-streamer status check, parallelized. Max 8 concurrent —
        # the global semaphore at 32 covers ALL sites so we leave
        # headroom for CB/CamSoda/etc. running concurrently.
        active: list = []
        for streamer in streamers:
            # Quick optimization: if a streamer is already PUBLIC or
            # RESTRICTED (already recording / about to record), skip
            # the status probe — the running recording loop handles
            # transitions on its own.
            if streamer.sc in (Status.PUBLIC, Status.RESTRICTED):
                continue
            active.append(streamer)
        if not active:
            return

        def _check_one(streamer):
            try:
                new_status = streamer.getStatus()
                streamer.setStatus(new_status)
                return None
            except Exception as e:
                # Don't crash the whole bulk pass if one streamer's
                # check raises — just leave that bot at its previous
                # status and move on.
                return f"{streamer.username}: {type(e).__name__}: {e}"

        # 8 workers × ~200ms each = ~25 seconds for 1000 bots.
        # Matches the 30s default poll interval — plenty of headroom.
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix='sc-bulk') as pool:
            errors = []
            for fut in as_completed(pool.submit(_check_one, s) for s in active):
                err = fut.result()
                if err:
                    errors.append(err)
            if errors and len(errors) > len(active) // 2:
                # More than half failed — likely a network issue, log so
                # the user can investigate (StripChat down? CF block?).
                # No log spam: only fires when something is genuinely
                # broken.
                # Use the first streamer's logger (they all share the
                # same handler chain).
                if streamers:
                    streamers[0].logger.warning(
                        f"StripChat bulk: {len(errors)}/{len(active)} "
                        f"per-username probes failed — first error: {errors[0]}"
                    )

    @classmethod
    def m3u_decoder(cls, content: str) -> str:
        @lru_cache(maxsize=64)
        def _decode(encrypted_b64: str, key: str) -> str:
            hash_bytes = cls._get_hash_bytes(key)
            data = base64.b64decode(encrypted_b64 + "==")
            return bytes(a ^ b for a, b in zip(data, itertools.cycle(hash_bytes))).decode()
        
        psch, pkey, pdkey = cls._getMouflonFromM3U(content)
        
        if not pdkey:
            return content
        
        lines = content.split('\n')
        decoded = []
        last_decoded = None
        
        # v1 decoding (FILE attribute)
        if psch == "v1":
            for line in lines:
                if line.startswith(cls._MOUFLON_FILE_ATTR):
                    last_decoded = _decode(line[len(cls._MOUFLON_FILE_ATTR):], pdkey)
                elif last_decoded and line.endswith(cls._MOUFLON_FILENAME):
                    decoded.append(line.replace(cls._MOUFLON_FILENAME, last_decoded))
                    last_decoded = None
                else:
                    decoded.append(line)
        
        # v2 decoding (URI attribute)
        elif psch == "v2":
            i = 0
            while i < len(lines):
                line = lines[i]
                line_stripped = line.strip()
                
                # Check for #EXT-X-MAP:URI lines (initialization segment)
                if line_stripped.startswith('#EXT-X-MAP:URI'):
                    try:
                        if '"' in line:
                            start_quote = line.find('"')
                            end_quote = line.rfind('"')
                            if start_quote >= 0 and end_quote > start_quote:
                                map_uri = line[start_quote+1:end_quote]
                                
                                if '_init_' in map_uri and '.mp4' in map_uri:
                                    uri_without_ext = map_uri[:-4]
                                    last_us = uri_without_ext.rfind('_')
                                    if last_us > 0:
                                        url_before_encrypted = uri_without_ext[:last_us]
                                        encrypted_part = uri_without_ext[last_us+1:]
                                        
                                        reversed_enc = encrypted_part[::-1]
                                        decoded_part = _decode(reversed_enc, pdkey)
                                        
                                        new_map_uri = f'{url_before_encrypted}_{decoded_part}.mp4'
                                        decoded.append(f'#EXT-X-MAP:URI="{new_map_uri}"')
                                        i += 1
                                        continue
                        
                        decoded.append(line)
                        i += 1
                        continue
                        
                    except Exception:
                        decoded.append(line)
                        i += 1
                        continue
                
                # Check for segment URI lines
                if line.startswith(cls._MOUFLON_URI_ATTR):
                    uri_value = line[len(cls._MOUFLON_URI_ATTR):]
                    
                    try:
                        if '.mp4' in uri_value:
                            last_underscore = uri_value.rfind('_')
                            if last_underscore > 0:
                                url_without_timestamp = uri_value[:last_underscore]
                                timestamp_part = uri_value[last_underscore+1:]
                                
                                second_last_underscore = url_without_timestamp.rfind('_')
                                if second_last_underscore > 0:
                                    url_before_encrypted = url_without_timestamp[:second_last_underscore]
                                    encrypted = url_without_timestamp[second_last_underscore+1:]
                                    
                                    reversed_encrypted = encrypted[::-1]
                                    decoded_segment = _decode(reversed_encrypted, pdkey)
                                    
                                    decoded_uri = f"{url_before_encrypted}_{decoded_segment}_{timestamp_part}"
                                    
                                    i += 1
                                    if i < len(lines):
                                        next_line = lines[i]
                                        if 'media.mp4' in next_line:
                                            decoded.append(decoded_uri)
                                            i += 1
                                            continue
                                        else:
                                            decoded.append(decoded_uri)
                                            continue
                                    else:
                                        decoded.append(decoded_uri)
                                        break
                                else:
                                    i += 1
                                    continue
                            else:
                                i += 1
                                continue
                        else:
                            i += 1
                            continue
                        
                    except Exception:
                        i += 1
                        continue
                else:
                    decoded.append(line)
                
                i += 1
        else:
            return content
        
        return '\n'.join(decoded)

    @classmethod
    @lru_cache(maxsize=128)
    def getMouflonDecKey(cls, pkey: str) -> Optional[str]:
        if pkey in cls._mouflon_keys:
            return cls._mouflon_keys[pkey]
        
        pattern = f'"{pkey}:'
        idx = cls._doppio_js_data.find(pattern)
        if idx != -1:
            start = idx + len(pattern)
            end = cls._doppio_js_data.find('"', start)
            if end != -1:
                key = cls._doppio_js_data[start:end]
                cls._mouflon_keys[pkey] = key
                # Persist newly-discovered keys so a StripChat key rotation
                # (upstream issue #359 "pkey has changed again") survives
                # restarts instead of being re-discovered every launch.
                cls._saveMouflonKeysToCache()
                return key

        return None

    @staticmethod
    def _getMouflonFromM3U(m3u8_doc: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        needle = StripChat._MOUFLON_NEEDLE
        idx = 0
        
        while (idx := m3u8_doc.find(needle, idx)) != -1:
            line_end = m3u8_doc.find('\n', idx)
            if line_end == -1:
                line_end = len(m3u8_doc)
            
            line = m3u8_doc[idx:line_end]
            parts = line.split(':', 3)
            
            if len(parts) >= 4:
                psch, pkey = parts[2], parts[3]
                if pdkey := StripChat.getMouflonDecKey(pkey):
                    return psch, pkey, pdkey
            
            idx += len(needle)
        
        return None, None, None

    @staticmethod
    def uniq():
        """Generate a random unique string for API requests."""
        chars = ''.join(chr(i) for i in range(ord('a'), ord('z') + 1))
        chars += ''.join(chr(i) for i in range(ord('0'), ord('9') + 1))
        return ''.join(random.choice(chars) for _ in range(16))

    @staticmethod
    def normalizeInfo(raw: dict) -> dict:
        """
        Normalize JSON so lastInfo is always a dict.
        Keep top-level where possible; flatten only obvious wrappers like `item` or single-element lists.
        """
        if not raw:
            return {}
        # If it's a list, use first element (common wrapping)
        if isinstance(raw, list):
            return raw[0] if raw else {}
        # If it's a dict with 'item' containing the real object
        if isinstance(raw, dict) and "item" in raw and isinstance(raw["item"], dict):
            return raw["item"]
        # If it's a dict with a single top wrapper like {"data": {...}} try common keys but avoid dropping cam/user
        if isinstance(raw, dict) and len(raw) == 1:
            sole_key = next(iter(raw))
            if sole_key in ("data", "result", "response") and isinstance(raw[sole_key], dict):
                return raw[sole_key]
        # Otherwise return as-is (we'll handle nested shapes in getters)
        return raw

    def _get_by_path(self, data: dict, path: list):
        """
        Safely get nested value by path list from dict-like structures.
        Returns None if any step is missing or type mismatch.
        """
        cur = data
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                return None
        return cur

    def _recursive_find(self, data, key):
        """
        Depth-first search for first occurrence of `key` in nested dict/list objects.
        Returns the value if found, otherwise None.
        """
        if isinstance(data, dict):
            if key in data:
                return data[key]
            for v in data.values():
                found = self._recursive_find(v, key)
                if found is not None:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = self._recursive_find(item, key)
                if found is not None:
                    return found
        return None

    def _first_in_paths(self, paths: list):
        """
        Try a list of paths (each path is a list of keys). Return first non-None value.
        """
        for p in paths:
            val = self._get_by_path(self.lastInfo, p)
            if val is not None:
                return val
        return None
    

    def getWebsiteURL(self):
        return "https://stripchat.com/" + self.username

    def getVideoUrl(self):
        return self.getWantedResolutionPlaylist(None)

    def getStreamName(self) -> str:
        """
        Robust streamName detector: checks multiple common locations and finally
        performs a recursive search for the key if not found.
        Raises KeyError if nothing found.
        """
        if not self.lastInfo:
            raise KeyError("lastInfo is empty, call getStatus() first")

        # Common paths to check in order
        paths = [
            ["streamName"],
            ["cam", "streamName"],
            ["user", "streamName"],
            ["user", "user", "streamName"],
            ["model", "streamName"],
            ["user", "user", "userStreamName"],
            ["cam", "userStreamName"],
        ]
        val = self._first_in_paths(paths)
        if val:
            return str(val)

        # Last resort: recursive find
        val = self._recursive_find(self.lastInfo, "streamName")
        if val:
            return str(val)

        raise KeyError(f"No streamName in lastInfo: keys={list(self.lastInfo.keys())}")

    def getStatusField(self):
        """
        Robust status detector. Tries known paths and falls back to recursive search.
        Returns the raw status value or None.
        """
        if not self.lastInfo:
            return None

        # Explicit candidate paths (ordered)
        paths = [
            ["status"],
            ["cam", "streamStatus"],
            ["cam", "status"],
            ["model", "status"],
            ["user", "status"],
            ["user", "user", "status"],
            ["user", "user", "state"],
            ["user", "user", "broadcastStatus"],
        ]
        status = self._first_in_paths(paths)
        if status is not None:
            return status

        # Recursive fallback — but prefer strings that match expected status tokens
        found = self._recursive_find(self.lastInfo, "status")
        if isinstance(found, str):
            return found
        return None

    def getIsLive(self) -> bool:
        """
        Robust isLive detector. Checks multiple locations and returns boolean.
        """
        if not self.lastInfo:
            return False

        # Try direct root flag
        val = self._get_by_path(self.lastInfo, ["isLive"])
        if val is not None:
            return bool(val)

        # Common locations
        paths = [
            ["cam", "isCamActive"],
            ["cam", "isCamAvailable"],
            ["cam", "isLive"],
            ["model", "isLive"],
            ["user", "isLive"],
            ["user", "user", "isLive"],
            ["user", "user", "isCamActive"],
            ["broadcastSettings", "isLive"],
            ["cam", "broadcastSettings", "isCamActive"],
            ["cam", "broadcastSettings", "isLive"],
        ]
        val = self._first_in_paths(paths)
        if val is not None:
            return bool(val)

        # Recursive fallback for any key named isLive or isCamActive
        for k in ("isLive", "isCamActive", "isCamAvailable"):
            found = self._recursive_find(self.lastInfo, k)
            if found is not None:
                return bool(found)

        return False

    def getIsMobile(self) -> bool:
        """
        Robust isMobile detector. Checks multiple locations and returns boolean.
        """
        if not self.lastInfo:
            return False

        # Direct
        val = self._get_by_path(self.lastInfo, ["isMobile"])
        if val is not None:
            return bool(val)

        # Common paths
        paths = [
            ["model", "isMobile"],
            ["user", "isMobile"],
            ["user", "user", "isMobile"],
            ["broadcastSettings", "isMobile"],
            ["cam", "broadcastSettings", "isMobile"],
            ["cam", "isMobile"],
        ]
        val = self._first_in_paths(paths)
        if val is not None:
            return bool(val)

        # Recursive fallback
        found = self._recursive_find(self.lastInfo, "isMobile")
        if found is not None:
            return bool(found)

        return False

    def getIsGeoBanned(self) -> bool:
        """Check if user is geo-banned from viewing this model."""
        if not self.lastInfo:
            return False

        paths = [
            ["isGeoBanned"],
            ["user", "isGeoBanned"],
            ["user", "user", "isGeoBanned"],
        ]
        val = self._first_in_paths(paths)
        if val is not None:
            return bool(val)

        found = self._recursive_find(self.lastInfo, "isGeoBanned")
        return bool(found) if found is not None else False

    def getIsDeleted(self) -> bool:
        """Check if the model account has been deleted."""
        if not self.lastInfo:
            return False

        # Try common paths where isDeleted might be found
        paths = [
            ["isDeleted"],
            ["user", "isDeleted"],
            ["user", "user", "isDeleted"],
            ["model", "isDeleted"],
        ]
        val = self._first_in_paths(paths)
        if val is not None:
            return bool(val)

        # Recursive fallback
        found = self._recursive_find(self.lastInfo, "isDeleted")
        return bool(found) if found is not None else False

    # Guards the rename hop: one follow per getStatus() call, so a server that
    # points A->B->A can't spin us in a loop.
    _rename_in_progress = False

    def _followRename(self, body):
        """If `body` says this model renamed, adopt the new name and re-check.

        Returns the fresh Status after adopting the new username, or None if
        this wasn't a rename (caller then treats it as NOTEXIST).
        """
        if self._rename_in_progress:
            return None
        try:
            data = json.loads(body or "{}")
        except Exception:
            return None
        if not isinstance(data, dict) or data.get("error") != "Model not found":
            return None
        new_username = ((data.get("data") or {}).get("newUsername") or "").strip()
        if not new_username or new_username == self.username:
            return None

        old = self.username
        self.logger.info(f"Model renamed on StripChat: {old} -> {new_username}")
        # move_folder=True keeps this model's back catalogue together under the
        # new name instead of stranding it under the dead one.
        self.setUsername(new_username, move_folder=True)
        self._rename_in_progress = True
        try:
            return self.getStatus()
        finally:
            self._rename_in_progress = False

    def getStatus(self):
        """Check the current status of the model's stream.

        2026-05-09: classify network/timeout exceptions as RATELIMIT
        rather than letting them propagate to bot.py and become
        Status.ERROR. ERROR doesn't trigger adaptive backoff so bots
        that hit a transient network issue keep retrying every 30-40s
        and spam the log with `Error on downloading` until something
        clears. RATELIMIT triggers exponential backoff that gives the
        connection pool time to recover.

        Catches:
          - requests.exceptions.RequestException (timeout, ConnectionError, etc.)
          - curl_cffi's parallel exception tree (TimeoutError /
            ConnectionError / DNSError / CurlError → all subclass OSError),
            which doesn't inherit from RequestException
          - Any unexpected exception → ERROR (so we still notice real bugs)
        """
        # Prefer a room_id we already hold; otherwise resolve (and cache) it.
        # The old username-keyed endpoint now 418s for every streamer.
        model_id = self.room_id or self._resolveModelId(
            self.session, self.username, self.headers, self.logger)
        if not model_id:
            return Status.NOTEXIST
        if not self.room_id:
            self.room_id = str(model_id)
        url = (f'https://stripchat.com/api/front/v2/models/{model_id}/cam'
               f'?uniq={StripChat.uniq()}')
        try:
            # Plain headers: verified against the new endpoint. Sending
            # html_headers here would set Accept: text/html on a JSON API,
            # which is the wrong thing to ask a JSON endpoint for.
            r = self.session.get(url, headers=self.headers, bucket='api')
        except requests.exceptions.RequestException as e:
            self.logger.debug(f'Network error for {self.username}: '
                               f'{type(e).__name__}: {e}')
            return Status.RATELIMIT
        except (TimeoutError, ConnectionError, OSError) as e:
            # curl_cffi exceptions (used by CFSessionManager) live in a
            # parallel tree; same treatment as RequestException.
            self.logger.debug(f'Network/timeout {type(e).__name__} for '
                               f'{self.username}: {e}')
            return Status.RATELIMIT
        except Exception as e:
            self.logger.error(f'Unexpected error fetching status for '
                               f'{self.username} [{type(e).__name__}]: {e!r}')
            return Status.ERROR

        ct = (r.headers.get("content-type") or "").lower()
        body = r.text or ""

        # Handle HTTP errors
        if r.status_code == 404:
            # Upstream fix (lossless1024/StreaMonitor 1fe1fbe / 68a9c8b):
            # StripChat lets models RENAME. The old username then 404s with
            # {"error": "Model not found", "data": {"newUsername": "..."}} and
            # the bot would sit at NOTEXIST forever, silently missing every
            # future stream. Follow the pointer instead.
            renamed = self._followRename(body)
            if renamed is not None:
                return renamed
            return Status.NOTEXIST
        if r.status_code == 403:
            if looks_like_cf_html(body):
                self.logger.error(f'Cloudflare challenge (403) for {self.username}')
                return Status.CLOUDFLARE
            return Status.RESTRICTED
        if r.status_code == 429:
            self.logger.error(f'Rate limited (429) for {self.username}')
            return Status.RATELIMIT
        if r.status_code == 418:
            # StripChat answers a blocked exit IP with 418 and an EMPTY body.
            # None of the checks above matched it, so it fell through to the
            # JSON validation below and every model reported "Non-JSON
            # response" -- which reads like a parsing quirk and hid a hard
            # site-wide block for hours (659 models stuck UNKNOWN, recording
            # nothing, with no backoff and no rotation signal).
            #
            # RATELIMIT is the right state: it backs off exponentially AND
            # reports to the VPN auto-rotator, which can actually clear an
            # IP-reputation block by moving to a fresh exit.
            if not getattr(StripChat, '_warned_blocked', False):
                StripChat._warned_blocked = True
                self.logger.error(
                    f'StripChat is blocking this exit IP (HTTP {r.status_code}, '
                    f'empty body) - backing off and flagging for VPN rotation')
            else:
                self.logger.debug(f'still blocked (HTTP {r.status_code})')
            return Status.RATELIMIT
        if 400 <= r.status_code < 500:
            # Deliberately NOT folded into the 418 branch above: 4xx codes like
            # 405/406 mean OUR request is malformed, and backing off on them
            # would hide a client bug behind a retry loop instead of surfacing
            # it. Report the code and treat it as unknown, so it shows up.
            self.logger.error(
                f'Unhandled HTTP {r.status_code} for {self.username} '
                f'(content-type={ct or "none"!r}, {len(body)} bytes)')
            return Status.UNKNOWN
        if r.status_code >= 500:
            if looks_like_cf_html(body):
                self.logger.error(f'Cloudflare challenge ({r.status_code}) for {self.username}')
                return Status.CLOUDFLARE
            self.logger.error(f'Server error {r.status_code} for {self.username}')
            return Status.UNKNOWN

        # Validate JSON response
        if "application/json" not in ct or not body.strip():
            self.logger.warning(
                f'Non-JSON response for {self.username} '
                f'(HTTP {r.status_code}, content-type={ct or "none"!r})')
            return Status.UNKNOWN

        try:
            raw = r.json()
        except Exception as e:
            self.logger.error(f'Failed to parse JSON for {self.username}: {e}')
            return Status.UNKNOWN

        # Normalize and store info
        self.lastInfo = self.normalizeInfo(raw)

        # Check for geo-ban
        if self.getIsGeoBanned():
            return Status.RESTRICTED

        # Check if model account has been deleted
        if self.getIsDeleted():
            self.logger.warning(f'⚠️ Model account {self.username} has been deleted - this model will be auto-deregistered')
            return Status.DELETED

        # Get status and cam state
        status = self.getStatusField()
        is_cam_available = self._get_by_path(self.lastInfo, ["isCamAvailable"]) or \
                          self._get_by_path(self.lastInfo, ["cam", "isCamAvailable"]) or False
        is_cam_active = self._get_by_path(self.lastInfo, ["isCamActive"]) or \
                       self._get_by_path(self.lastInfo, ["cam", "isCamActive"]) or False

        # A model can be "public" + cam-active yet NOT actually broadcasting --
        # paused, between streams, or a ticket show -- with isLive False and an
        # EMPTY streamName. Marking that PUBLIC makes the recorder build a master
        # URL that 404s on every CDN host, burning retries into ERROR (this is
        # what stranded __Naomi at ERROR). Gate PUBLIC on a real stream: if
        # there's no streamName, it's not recordable -> treat as OFFLINE and wait
        # for the model to actually go live (the next poll picks it up).
        try:
            _stream_name = self.getStreamName()
        except Exception:
            _stream_name = None

        # Only return PUBLIC if status is public AND camera is available AND
        # active AND there is an actual stream to record.
        if status == "public" and is_cam_available and is_cam_active and _stream_name:
            return Status.PUBLIC
        if status == "public" and (is_cam_active or is_cam_available) and not _stream_name:
            return Status.OFFLINE

        if status in self._PRIVATE_STATUSES:
            return Status.PRIVATE

        if status in self._OFFLINE_STATUSES:
            return Status.OFFLINE

        # Unknown status - log the actual data for debugging
        self.logger.warning(f"Unknown status '{status}' for {self.username} - lastInfo keys: {list(self.lastInfo.keys())}")
        self.logger.debug(f"Full response for {self.username}: {str(self.lastInfo)[:500]}")
        return Status.UNKNOWN

    def isMobile(self):
        """Check if the current broadcast is from a mobile device."""
        return self.getIsMobile()

    def getPlaylistVariants(self, url):
        """Fetch HLS playlist variants with mouflon encryption keys."""
        s = self._get_session()
        stream_name = self.room_id

        if not stream_name:
            # A missing room_id is recoverable, not terminal: it just means
            # resolution failed when the bot was created (network blip during a
            # bulk add, or the model was offline then). Previously this only
            # logged an ERROR and returned, so the model re-hit it on every
            # poll and could NEVER record — one model produced an identical
            # error every 60s indefinitely. Try to resolve it now.
            try:
                resolved = self.getRoomIdFromUsername(self.username)
            except Exception as e:
                resolved = None
                self.logger.debug(f"room_id re-resolve failed: {e}")
            if resolved:
                self.room_id = stream_name = str(resolved)
                self._warned_no_room_id = False
                self.logger.info(f"Recovered missing room_id: {stream_name}")
            else:
                # Still nothing — say so once, then stay quiet. Repeating an
                # unchanged fact every poll buries real failures in the feed.
                if not getattr(self, "_warned_no_room_id", False):
                    self._warned_no_room_id = True
                    self.logger.warning(
                        "No room_id and could not resolve it from the username; "
                        "this model cannot record until it resolves")
                else:
                    self.logger.debug("still no room_id")
                return []
        
        # Check if stream is origin-only (not yet replicated to edge CDN)
        # This is a temporary state - retry a few times with delay
        max_origin_retries = 5
        origin_retry_delay = 3  # seconds
        
        for retry in range(max_origin_retries):
            origin_only = self._get_by_path(self.lastInfo, ["cam", "broadcastSettings", "originOnly"])
            if not origin_only:
                break
            
            if retry < max_origin_retries - 1:
                self.logger.info(f"Stream is origin-only (not on edge CDN yet), waiting {origin_retry_delay}s... (attempt {retry + 1}/{max_origin_retries})")
                time.sleep(origin_retry_delay)
                # Re-fetch status to check if originOnly changed
                try:
                    self.getStatus()
                except Exception as e:
                    self.logger.warning(f"Failed to re-fetch status: {e}")
                    break
            else:
                self.logger.warning(f"Stream still origin-only after {max_origin_retries} attempts - skipping for now")
                return []
        
        # Build playlist URL - try multiple CDN hosts (matching upstream's known hosts)
        cdn_hosts = ['doppiocdn.org', 'doppiocdn.com', 'doppiocdn.net']
        random.shuffle(cdn_hosts)
        
        vr_suffix = '_vr' if self.vr else ''
        auto_suffix = '_auto' if not self.vr else ''
        
        result = None
        playlist_url = None
        
        for host in cdn_hosts:
            playlist_url = f"https://edge-hls.{host}/hls/{stream_name}{vr_suffix}/master/{stream_name}{vr_suffix}{auto_suffix}.m3u8"
            self.debug(f"Fetching playlist from: {playlist_url}")
            
            try:
                result = s.get(playlist_url, headers=self.headers, cookies=self.cookies, timeout=10)
                self.debug(f"Playlist response from {host}: {result.status_code}")
                
                if result.status_code == 200:
                    break
                elif result.status_code == 404:
                    # Per-host failover noise (fires 3x for every offline model);
                    # the genuinely-stuck case escalates via the run-loop 3-strike.
                    self.logger.debug(f"Playlist not found on {host}, trying next CDN...")
                    result = None
                else:
                    self.logger.debug(f"Unexpected status {result.status_code} from {host}")
                    result = None
            except Exception as e:
                self.logger.debug(f"Failed to fetch from {host}: {e}")
                result = None
        
        if not result or result.status_code != 200:
            # Almost always the model left public between the status poll and this
            # fetch (master 404s on every host) or a private show (403) or a
            # transient doppiocdn DNS blip. The run loop escalates to ERROR (UI
            # badge) only after consecutive failures, so this per-blip line is
            # debug-only -- it was firing ~per second across the SC fleet.
            self.logger.debug("Failed to fetch playlist from any CDN host (model likely left / private)")
            return []
            
        m3u8_doc = result.text
        self.debug(f"M3U8 content (first 300 chars): {m3u8_doc[:300]}")
        
        psch, pkey, pdkey = StripChat._getMouflonFromM3U(m3u8_doc)
        self.debug(f"Extracted key psch={psch}, pkey={pkey}, pdkey={pdkey}")
        
        if not pkey:
            self.logger.error("No mouflon pkey available - keys not extracted at startup?")
            self.debug(f"Known mouflon keys: {list(StripChat._mouflon_keys.keys())}")
            return []
        
        variants = super().getPlaylistVariants(m3u_data=m3u8_doc)
        self.debug(f"Parsed {len(variants) if variants else 0} variants from playlist")
        
        if not variants:
            self.logger.debug("No variants found in playlist (transient)")
            return []
        
        # Add authentication keys to variant URLs and rewrite to use direct CDN
        # The master playlist returns media-hls.doppiocdn.X/b-hls-XX/... which is blocked (403)
        # We need to rewrite to b-hls-XX.doppiocdn.live/hls/... which works
        result = []
        for variant in variants:
            url = variant['url']
            # A previous fork-only rewrite turned the master playlist's
            # media-hls.doppiocdn.{org,com,net} variant URLs into
            # b-hls-XX.doppiocdn.live/... — but that CDN domain has been
            # RETIRED and no longer resolves (NXDOMAIN), so every segment fetch
            # failed with "getaddrinfo failed". Upstream (lossless1024) uses the
            # variant URLs exactly as the master returns them and only appends
            # the mouflon auth params — the original media-hls/edge-hls hosts
            # resolve fine. Do the same: keep the host, just add the keys.
            if 'pkey=' not in url or 'pdkey=' not in url:
                sep = '&' if '?' in url else '?'
                url = f"{url}{sep}psch={psch}&pkey={pkey}&pdkey={pdkey}"
            result.append(variant | {"url": url})

        return result
