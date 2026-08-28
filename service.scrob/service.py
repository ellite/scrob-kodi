import json
import os
import sys
import threading
import time
import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

try:
    from urllib.request import Request, urlopen
    from urllib.parse import quote
    from urllib.error import HTTPError
except ImportError:
    from urllib2 import Request, urlopen, HTTPError
    from urllib import quote

ADDON_ID = 'service.scrob'

DEVICE_GRANT_TYPE = 'urn:ietf:params:oauth:grant-type:device_code'


def _settings():
    a = xbmcaddon.Addon(id=ADDON_ID)
    return {
        'url': a.getSetting('scrob_url').rstrip('/'),
        'key': a.getSetting('api_key').strip(),
        'interval': max(10, int(a.getSetting('progress_interval') or 60)),
        'sync_to_scrob': a.getSettingBool('sync_to_scrob'),
        'sync_from_scrob': a.getSettingBool('sync_from_scrob'),
        'sync_ratings_from_scrob': a.getSettingBool('sync_ratings_from_scrob'),
        'sync_interval': max(0, int(a.getSetting('sync_interval') or 0)),
        'watched_threshold': min(100, max(50, int(a.getSetting('watched_threshold') or 90))),
        'rate_on_completion': a.getSettingBool('rate_on_completion'),
        'rate_episodes_on_completion': a.getSettingBool('rate_episodes_on_completion'),
    }


# ── Auth / HTTP ───────────────────────────────────────────────────────────────
#
# The add-on talks to Scrob with one of two credentials, in this order:
#   1. an OAuth 2.0 device-grant token (RFC 8628), obtained via "Authorize with
#      Scrob" in the settings and stored (with its refresh token) in auth.json;
#   2. the legacy account API key from the settings.
# The API key path is kept so that instances predating device linking — and
# add-on installs that are never reconfigured — keep working unchanged.

class _HTTPError(Exception):
    def __init__(self, status, body):
        super(_HTTPError, self).__init__('HTTP {}'.format(status))
        self.status = status
        self.body = body if isinstance(body, dict) else {}


def _http_json(url, payload=None, headers=None, timeout=15):
    h = {'Accept': 'application/json'}
    if payload is not None:
        h['Content-Type'] = 'application/json'
    if headers:
        h.update(headers)
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    try:
        resp = urlopen(Request(url, data=data, headers=h), timeout=timeout)
        raw = resp.read().decode('utf-8')
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode('utf-8'))
        except Exception:
            body = {}
        raise _HTTPError(getattr(exc, 'code', 0), body)
    return json.loads(raw) if raw else {}


def _token_path():
    prof = xbmcvfs.translatePath(xbmcaddon.Addon(id=ADDON_ID).getAddonInfo('profile'))
    if not xbmcvfs.exists(prof):
        xbmcvfs.mkdirs(prof)
    return os.path.join(prof, 'auth.json')


def _load_tokens():
    try:
        p = _token_path()
        if not xbmcvfs.exists(p):
            return {}
        f = xbmcvfs.File(p)
        try:
            return json.loads(f.read() or '{}') or {}
        finally:
            f.close()
    except Exception:
        return {}


def _save_tokens(resp):
    data = {
        'access_token': resp.get('access_token'),
        'refresh_token': resp.get('refresh_token') or _load_tokens().get('refresh_token'),
        'expires_at': time.time() + max(60, int(resp.get('expires_in') or 3600)) - 60,
    }
    try:
        f = xbmcvfs.File(_token_path(), 'w')
        try:
            f.write(json.dumps(data))
        finally:
            f.close()
    except Exception as exc:
        xbmc.log('[service.scrob] could not persist tokens: {}'.format(exc), xbmc.LOGWARNING)
    return data


def _delete_tokens():
    try:
        p = _token_path()
        if xbmcvfs.exists(p):
            xbmcvfs.delete(p)
    except Exception:
        pass


_refresh_lock = threading.Lock()


def _refresh_token(stale_access=None):
    s = _settings()
    with _refresh_lock:
        tok = _load_tokens()
        cur, rt = tok.get('access_token'), tok.get('refresh_token')
        if not rt or not s['url']:
            return None
        # Another thread may have refreshed while we waited for the lock.
        if cur and cur != stale_access and time.time() < tok.get('expires_at', 0):
            return cur
        try:
            resp = _http_json('{}/api/proxy/auth/device/token'.format(s['url']),
                              {'grant_type': 'refresh_token', 'refresh_token': rt})
        except _HTTPError as exc:
            err = exc.body.get('error')
            xbmc.log('[service.scrob] token refresh rejected: {}'.format(err or exc.status), xbmc.LOGWARNING)
            if err in ('invalid_grant', 'invalid_request', 'unauthorized_client'):
                _delete_tokens()
            return None
        except Exception as exc:
            xbmc.log('[service.scrob] token refresh failed: {}'.format(exc), xbmc.LOGWARNING)
            return None
        return _save_tokens(resp).get('access_token')


def _access_token():
    tok = _load_tokens()
    at = tok.get('access_token')
    if not at:
        return None
    if time.time() < tok.get('expires_at', 0):
        return at
    return _refresh_token()


def _api(path, payload=None, timeout=15):
    """Call {url}/api/proxy/{path} — GET when payload is None, else POST.
    Authenticates with the device token when the add-on has been authorized,
    otherwise the legacy API key. Returns the parsed JSON body, or None on any
    failure."""
    s = _settings()
    if not s['url']:
        return None
    token = _access_token()
    if not token and not s['key']:
        xbmc.log('[service.scrob] not connected to Scrob (not authorized, no API key)', xbmc.LOGWARNING)
        return None

    def _call(bearer):
        url = '{}/api/proxy/{}'.format(s['url'], path)
        headers = {}
        if bearer:
            headers['Authorization'] = 'Bearer ' + bearer
        else:
            url += ('&' if '?' in url else '?') + 'api_key=' + quote(s['key'], safe='')
        return _http_json(url, payload, headers, timeout)

    try:
        return _call(token)
    except _HTTPError as exc:
        if exc.status == 401 and token:
            fresh = _refresh_token(stale_access=token)
            if fresh:
                try:
                    return _call(fresh)
                except Exception as retry_exc:
                    xbmc.log('[service.scrob] request retry failed: {}'.format(retry_exc), xbmc.LOGWARNING)
                    return None
        xbmc.log('[service.scrob] request failed: HTTP {} ({})'.format(exc.status, path), xbmc.LOGWARNING)
        return None
    except Exception as exc:
        xbmc.log('[service.scrob] request failed: {} ({})'.format(exc, path), xbmc.LOGWARNING)
        return None


def _connected():
    return bool(_access_token()) or bool(_settings()['key'])


def _client_name():
    name = ''
    try:
        name = xbmc.getInfoLabel('System.FriendlyName') or ''
    except Exception:
        pass
    return u'Kodi – {}'.format(name) if name and name.lower() != 'kodi' else 'Kodi'


def _device_authorize():
    s = _settings()
    dialog = xbmcgui.Dialog()
    if not s['url']:
        dialog.ok('Scrob', 'Set your Scrob URL in the add-on settings first.')
        return
    try:
        start = _http_json('{}/api/proxy/auth/device/code'.format(s['url']),
                           {'client_name': _client_name(), 'scope': 'write'})
    except _HTTPError as exc:
        dialog.ok('Scrob', 'Could not start authorization (HTTP {}).\n'
                           'Check the Scrob URL, and that your Scrob instance supports device linking.'.format(exc.status))
        return
    except Exception as exc:
        dialog.ok('Scrob', 'Could not reach Scrob:\n{}'.format(exc))
        return

    device_code = start.get('device_code')
    user_code = start.get('user_code', '')
    verify = start.get('verification_uri') or '{}/link'.format(s['url'])
    interval = max(2, int(start.get('interval') or 5))
    expires_in = max(60, int(start.get('expires_in') or 900))
    if not device_code or not user_code:
        dialog.ok('Scrob', 'Scrob returned an unexpected response.')
        return

    msg = ('On a phone or computer, open:\n[B]{}[/B]\n\nand enter the code:  [B]{}[/B]').format(verify, user_code)
    progress = xbmcgui.DialogProgress()
    progress.create('Authorize Scrob', msg)

    deadline = time.time() + expires_in
    outcome = None
    while time.time() < deadline and not progress.iscanceled():
        progress.update(max(0, min(99, int(100 * (1 - (deadline - time.time()) / expires_in)))), msg)
        for _ in range(interval):
            if progress.iscanceled():
                break
            xbmc.sleep(1000)
        if progress.iscanceled():
            break
        try:
            token = _http_json('{}/api/proxy/auth/device/token'.format(s['url']),
                               {'grant_type': DEVICE_GRANT_TYPE, 'device_code': device_code})
        except _HTTPError as exc:
            err = exc.body.get('error')
            if err == 'authorization_pending':
                continue
            if err == 'slow_down':
                interval += 5
                continue
            outcome = err or 'error'
            break
        except Exception:
            continue
        if token.get('access_token'):
            _save_tokens(token)
            outcome = 'ok'
            break

    progress.close()
    _update_connection_status()
    if outcome == 'ok':
        dialog.notification('Scrob', u'Authorized ✓', xbmcgui.NOTIFICATION_INFO)
    elif outcome == 'access_denied':
        dialog.notification('Scrob', 'Authorization was declined', xbmcgui.NOTIFICATION_WARNING)
    elif outcome in ('expired_token', 'invalid_grant'):
        dialog.notification('Scrob', 'The code expired — please try again', xbmcgui.NOTIFICATION_WARNING)
    elif outcome:
        dialog.notification('Scrob', 'Authorization failed', xbmcgui.NOTIFICATION_ERROR)


def _forget_authorization():
    dialog = xbmcgui.Dialog()
    if not _load_tokens().get('access_token'):
        dialog.notification('Scrob', 'Not authorized on this device', xbmcgui.NOTIFICATION_INFO)
        return
    if dialog.yesno('Scrob', 'Forget the Scrob authorization on this device?'):
        _delete_tokens()
        _update_connection_status()
        dialog.notification('Scrob', 'Signed out', xbmcgui.NOTIFICATION_INFO)


def _connection_status_text():
    """One-line summary of how the add-on will authenticate to Scrob, shown
    read-only at the top of the Connection settings."""
    if _load_tokens().get('access_token'):
        return 'Authorized with Scrob on this device'
    if _settings()['key']:
        return 'Connected with an API key'
    return 'Not connected — use "Authorize with Scrob", or set an API key'


def _update_connection_status():
    try:
        a = xbmcaddon.Addon(id=ADDON_ID)
        text = _connection_status_text()
        # Only write on a real change — setSetting re-fires onSettingsChanged,
        # which calls back here.
        if a.getSetting('conn_status') != text:
            a.setSetting('conn_status', text)
    except Exception:
        pass


# ── Scrobbling ────────────────────────────────────────────────────────────────

def _post(method, item, player_state, ended=False):
    if not _settings()['sync_to_scrob']:
        return
    payload = {'method': method, 'item': item, 'player': player_state}
    if method == 'Player.OnStop':
        payload['params'] = {'data': {'end': ended}}
    if _api('webhooks/kodi', payload) is not None:
        xbmc.log('[service.scrob] {}'.format(method), xbmc.LOGDEBUG)


def _post_rating(item, rating):
    uid = item.get('uniqueid', {})
    payload = {
        'media_type': item['type'],
        'title': item.get('title', ''),
        'year': item.get('year'),
        'tmdb_id': uid.get('tmdb'),
        'imdb_id': uid.get('imdb'),
        'tvdb_id': uid.get('tvdb'),
        'rating': float(rating),
        'series_name': item.get('showtitle'),
        'season_number': item.get('season'),
        'episode_number': item.get('episode'),
    }
    if _api('webhooks/kodi/rating', payload) is not None:
        xbmc.log('[service.scrob] Rating submitted: {}'.format(rating), xbmc.LOGDEBUG)


def _ask_and_post_rating(item):
    xbmc.sleep(1500)
    if item['type'] == 'episode':
        label = '{} S{:02d}E{:02d}'.format(
            item.get('showtitle', ''), item.get('season', 0), item.get('episode', 0))
    else:
        label = item.get('title', '')
    options = ['{0} {1}'.format(i, u'★' * i) for i in range(1, 11)]
    idx = xbmcgui.Dialog().select('Rate: {}'.format(label), options)
    if idx >= 0:
        _post_rating(item, idx + 1)


def _kodi_rpc(method, params=None):
    query = json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params or {}, 'id': 1})
    return json.loads(xbmc.executeJSONRPC(query)).get('result', {})


def _sync_from_scrob(monitor=None):
    """Pull watch counts (and, optionally, ratings) from Scrob into the local
    Kodi library. Runs on startup and, if a sync interval is configured, on that
    interval so several Kodi boxes sharing one Scrob account converge over time.

    Writing playcount/userrating back into the library makes Kodi emit
    VideoLibrary.OnUpdate for every touched item; ``monitor`` lets us mute the
    add-on's own OnUpdate handler for the duration so those echoes don't get
    scrobbled straight back to Scrob.
    """
    s = _settings()
    if not _connected():
        xbmc.log('[service.scrob] not connected to Scrob — skipping sync', xbmc.LOGWARNING)
        return

    def _mute(seconds=90):
        if monitor is not None:
            monitor._suppress_onupdate_until = time.time() + seconds

    _mute(600)

    library = _api('webhooks/kodi/history', timeout=30)
    if library is None:
        xbmc.log('[service.scrob] Sync from Scrob failed', xbmc.LOGWARNING)
        _mute(5)
        return

    ratings = {'movies': [], 'episodes': []}
    if s['sync_ratings_from_scrob']:
        fetched = _api('webhooks/kodi/ratings', timeout=30)
        if fetched is not None:
            ratings = fetched

    def _want_rating(value):
        try:
            return min(10, max(0, int(round(float(value or 0)))))
        except (TypeError, ValueError):
            return 0

    # ── Movies — match by TMDB ID ──────────────────────────────────────────────
    scrob_movies = {str(m['tmdb_id']): int(m['play_count'] or 0) for m in library.get('movies', [])}
    scrob_movie_ratings = {str(m['tmdb_id']): m['rating'] for m in ratings.get('movies', [])}
    if scrob_movies or scrob_movie_ratings:
        kodi_movies = _kodi_rpc('VideoLibrary.GetMovies',
                                {'properties': ['uniqueid', 'playcount', 'userrating']}).get('movies', [])
        for km in kodi_movies:
            tmdb_id = str(km.get('uniqueid', {}).get('tmdb', '') or '')
            if not tmdb_id:
                continue
            changes = {}
            if scrob_movies.get(tmdb_id, 0) > km.get('playcount', 0):
                changes['playcount'] = scrob_movies[tmdb_id]
            want = _want_rating(scrob_movie_ratings.get(tmdb_id))
            if want >= 1 and not km.get('userrating', 0):
                changes['userrating'] = want
            if changes:
                changes['movieid'] = km['movieid']
                _kodi_rpc('VideoLibrary.SetMovieDetails', changes)
                _mute()

    # ── Episodes — match by show TMDB ID + season + episode number ─────────────
    scrob_eps = {
        (str(e['show_tmdb_id']), e['season_number'], e['episode_number']): int(e['play_count'] or 0)
        for e in library.get('episodes', [])
    }
    scrob_ep_ratings = {
        (str(e['show_tmdb_id']), e['season_number'], e['episode_number']): e['rating']
        for e in ratings.get('episodes', [])
    }
    if scrob_eps or scrob_ep_ratings:
        kodi_shows = _kodi_rpc('VideoLibrary.GetTVShows',
                               {'properties': ['uniqueid']}).get('tvshows', [])
        show_tmdb_by_id = {}
        for ks in kodi_shows:
            tmdb_id = str(ks.get('uniqueid', {}).get('tmdb', '') or '')
            if tmdb_id:
                show_tmdb_by_id[ks['tvshowid']] = tmdb_id

        kodi_eps = _kodi_rpc('VideoLibrary.GetEpisodes',
                             {'properties': ['tvshowid', 'season', 'episode', 'playcount', 'userrating']}).get('episodes', [])
        for ke in kodi_eps:
            tmdb_id = show_tmdb_by_id.get(ke['tvshowid'])
            if not tmdb_id:
                continue
            k = (tmdb_id, ke['season'], ke['episode'])
            changes = {}
            if scrob_eps.get(k, 0) > ke.get('playcount', 0):
                changes['playcount'] = scrob_eps[k]
            want = _want_rating(scrob_ep_ratings.get(k))
            if want >= 1 and not ke.get('userrating', 0):
                changes['userrating'] = want
            if changes:
                changes['episodeid'] = ke['episodeid']
                _kodi_rpc('VideoLibrary.SetEpisodeDetails', changes)
                _mute()

    _mute(20)
    xbmc.log('[service.scrob] Library sync from Scrob complete', xbmc.LOGINFO)


def _hms(secs):
    secs = max(0, int(secs or 0))
    return {'hours': secs // 3600, 'minutes': (secs % 3600) // 60, 'seconds': secs % 60}


def _seconds(hms):
    hms = hms or {}
    return hms.get('hours', 0) * 3600 + hms.get('minutes', 0) * 60 + hms.get('seconds', 0)


def _progress_pct(player_state):
    if not player_state:
        return 0.0
    total = _seconds(player_state.get('totaltime'))
    return (_seconds(player_state.get('time')) / total) if total else 0.0


def _read_item(player):
    """Read the current player item via JSON-RPC and position. Returns (item, player_state) or (None, None)."""
    try:
        result = _kodi_rpc('Player.GetItem', {
            'playerid': 1,
            'properties': ['title', 'showtitle', 'season', 'episode', 'year', 'uniqueid'],
        })
        kodi_item = result.get('item', {})
    except Exception:
        return None, None

    media_type = kodi_item.get('type', '')
    if media_type not in ('movie', 'episode'):
        return None, None

    uid = kodi_item.get('uniqueid') or {}

    item = {'type': media_type, 'uniqueid': uid}
    if media_type == 'movie':
        item['title'] = kodi_item.get('title', '')
        item['year'] = kodi_item.get('year')
    else:
        item['title'] = kodi_item.get('title', '')
        item['showtitle'] = kodi_item.get('showtitle', '')
        item['season'] = kodi_item.get('season', 0)
        item['episode'] = kodi_item.get('episode', 0)

    try:
        pos = player.getTime()
        total = player.getTotalTime()
    except Exception:
        pos, total = 0, 0

    return item, {'time': _hms(pos), 'totaltime': _hms(total)}


class _ProgressThread(threading.Thread):
    def __init__(self, player, interval, on_sample=None):
        super(_ProgressThread, self).__init__()
        self.daemon = True
        self._player = player
        self._interval = interval
        self._on_sample = on_sample
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self._interval):
            if not self._player.isPlayingVideo():
                break
            item, ps = _read_item(self._player)
            if item:
                # Keep the monitor's cached position fresh so a later OnStop
                # (where the player is already gone) knows how far playback got.
                if self._on_sample:
                    self._on_sample(item, ps)
                _post('Player.OnAVChange', item, ps)


class ScrobMonitor(xbmc.Monitor):
    _SCROBBLE_TTL = 900

    def __init__(self):
        super(ScrobMonitor, self).__init__()
        self._player = xbmc.Player()
        self._lock = threading.Lock()
        self._thread = None
        self._last_item = None
        self._last_ps = None
        self._scrobbled = {}
        self._suppress_onupdate_until = 0

    def _cache(self, item, ps):
        with self._lock:
            self._last_item = item
            self._last_ps = ps

    def _cached(self):
        with self._lock:
            return self._last_item, self._last_ps

    @staticmethod
    def _media_key(item):
        if not item:
            return None
        if item.get('type') == 'movie':
            tmdb = (item.get('uniqueid') or {}).get('tmdb')
            if tmdb:
                return ('movie', str(tmdb))
            return ('movie', (item.get('title', '') or '').lower(), item.get('year'))
        return ('episode', (item.get('showtitle', '') or '').lower(),
                item.get('season'), item.get('episode'))

    def _mark_scrobbled(self, item):
        key = self._media_key(item)
        if not key:
            return
        with self._lock:
            self._scrobbled[key] = time.time()
            if len(self._scrobbled) > 500:
                cutoff = time.time() - self._SCROBBLE_TTL
                self._scrobbled = {k: v for k, v in self._scrobbled.items() if v >= cutoff}

    def _recently_scrobbled(self, item):
        key = self._media_key(item)
        if not key:
            return False
        with self._lock:
            return (time.time() - self._scrobbled.get(key, 0)) < self._SCROBBLE_TTL

    def _start_progress(self):
        self._stop_progress()
        s = _settings()
        self._thread = _ProgressThread(self._player, s['interval'], on_sample=self._cache)
        self._thread.start()

    def _stop_progress(self):
        if self._thread:
            self._thread.stop()
            self._thread = None

    def _library_item(self, lib_type, lib_id):
        """Build a scrobble ``item`` (and a 100%-watched player_state) for a
        library row identified by a VideoLibrary.OnUpdate notification."""
        if lib_type == 'movie':
            d = _kodi_rpc('VideoLibrary.GetMovieDetails', {
                'movieid': lib_id,
                'properties': ['title', 'year', 'uniqueid', 'runtime'],
            }).get('moviedetails') or {}
            if not d:
                return None, None
            item = {'type': 'movie', 'id': lib_id, 'uniqueid': d.get('uniqueid') or {},
                    'title': d.get('title', ''), 'year': d.get('year')}
            runtime = int(d.get('runtime') or 0) or 3600
        else:
            d = _kodi_rpc('VideoLibrary.GetEpisodeDetails', {
                'episodeid': lib_id,
                'properties': ['title', 'showtitle', 'season', 'episode', 'uniqueid', 'runtime'],
            }).get('episodedetails') or {}
            if not d:
                return None, None
            item = {'type': 'episode', 'id': lib_id, 'uniqueid': d.get('uniqueid') or {},
                    'title': d.get('title', ''), 'showtitle': d.get('showtitle', ''),
                    'season': d.get('season', 0), 'episode': d.get('episode', 0)}
            runtime = int(d.get('runtime') or 0) or 1800
        return item, {'time': _hms(runtime), 'totaltime': _hms(runtime)}

    def _handle_library_update(self, data):
        """React to a manual 'mark as watched' (or Kodi auto-marking a title
        watched near the end of playback) by scrobbling it to Scrob."""
        if not _settings()['sync_to_scrob'] or not _connected():
            return
        if time.time() < self._suppress_onupdate_until:
            return
        try:
            d = json.loads(data or '{}')
        except Exception:
            return
        # 'playcount' is only present when the update *is* a playcount change.
        if int(d.get('playcount') or 0) < 1:
            return
        lib = d.get('item') or {}
        lib_type, lib_id = lib.get('type'), lib.get('id')
        if lib_type not in ('movie', 'episode') or lib_id is None:
            return

        item, ps = self._library_item(lib_type, lib_id)
        if not item:
            return
        # Already scrobbled as complete by the playback's own OnStop (or a
        # previous OnUpdate for the same title) — nothing to add.
        if self._recently_scrobbled(item):
            return

        xbmc.log('[service.scrob] Scrobbling mark-as-watched: {}'.format(
            self._media_key(item)), xbmc.LOGINFO)
        _post('Player.OnStop', item, ps, ended=True)
        self._mark_scrobbled(item)

    def onSettingsChanged(self):
        _update_connection_status()

    def onNotification(self, sender, method, data):
        if method == 'Player.OnPlay':
            item, ps = None, None
            for _ in range(6):
                xbmc.sleep(500)
                item, ps = _read_item(self._player)
                if item:
                    break
            if item:
                self._cache(item, ps)
                _post('Player.OnPlay', item, ps)
                self._start_progress()

        elif method == 'Player.OnResume':
            item, ps = _read_item(self._player)
            if item:
                self._cache(item, ps)
                _post('Player.OnResume', item, ps)
                self._start_progress()

        elif method == 'Player.OnPause':
            self._stop_progress()
            item, ps = _read_item(self._player)
            if not item:
                item, ps = self._cached()
            if item:
                self._cache(item, ps)
                _post('Player.OnPause', item, ps)

        elif method == 'Player.OnStop':
            self._stop_progress()
            item, ps = self._cached()
            if item:
                try:
                    ended = json.loads(data or '{}').get('end', False)
                except Exception:
                    ended = False
                s = _settings()
                if not ended and _progress_pct(ps) >= s['watched_threshold'] / 100.0:
                    ended = True
                # Kodi's own playcount bump (VideoLibrary.OnUpdate) may have
                # raced ahead and already scrobbled this as complete.
                if not ended and self._recently_scrobbled(item):
                    ended = True
                _post('Player.OnStop', item, ps, ended=ended)
                if ended:
                    self._mark_scrobbled(item)
                    if (item['type'] == 'movie' and s['rate_on_completion']) or \
                       (item['type'] == 'episode' and s['rate_episodes_on_completion']):
                        t = threading.Thread(target=_ask_and_post_rating, args=(item,))
                        t.daemon = True
                        t.start()
                self._cache(None, None)

        elif method in ('Player.OnSeek', 'Player.OnAVChange'):
            item, ps = _read_item(self._player)
            if item:
                self._cache(item, ps)
                _post('Player.OnAVChange', item, ps)
                if not self._thread or not self._thread.is_alive():
                    self._start_progress()

        elif method == 'VideoLibrary.OnUpdate':
            try:
                self._handle_library_update(data)
            except Exception as exc:
                xbmc.log('[service.scrob] OnUpdate handling failed: {}'.format(exc), xbmc.LOGWARNING)


def run():
    xbmc.log('[service.scrob] Starting', xbmc.LOGINFO)
    _update_connection_status()
    monitor = ScrobMonitor()
    if _settings()['sync_from_scrob']:
        _sync_from_scrob(monitor)
    last_sync = time.time()
    while not monitor.abortRequested():
        if monitor.waitForAbort(30):
            break
        s = _settings()
        if s['sync_from_scrob'] and s['sync_interval'] > 0 \
           and (time.time() - last_sync) >= s['sync_interval'] * 60:
            _sync_from_scrob(monitor)
            last_sync = time.time()
    monitor._stop_progress()
    xbmc.log('[service.scrob] Stopped', xbmc.LOGINFO)


# Entry point. Kodi starts this file as the background service with no
# arguments; the settings "Authorize" / "Sign out" buttons re-invoke it via
# RunScript with an action argument.
_ACTION = sys.argv[1] if len(sys.argv) > 1 else ''
if _ACTION == 'authorize':
    _device_authorize()
elif _ACTION == 'signout':
    _forget_authorization()
else:
    run()
