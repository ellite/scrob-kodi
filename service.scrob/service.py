import json
import os
import re
import sys
import threading
import time
import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

try:
    from urllib.request import Request, urlopen
    from urllib.parse import quote, unquote
    from urllib.error import HTTPError
except ImportError:
    from urllib2 import Request, urlopen, HTTPError
    from urllib import quote, unquote

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
        'rating_default': min(10, max(1, int(a.getSetting('rating_default') or 5))),
        'rate_episodes_on_completion': a.getSettingBool('rate_episodes_on_completion'),
    }


# ── Excluded paths ────────────────────────────────────────────────────────────
#
# Playback of anything under an excluded folder or source is never sent to Scrob
# (events, progress, mark-as-watched, ratings). The list is stored as a JSON array
# in exclusions.json in the add-on profile, not in a Kodi setting: the settings
# dialog keeps its own copy of the values and can discard changes made by a script.

def _exclusions_path():
    prof = xbmcvfs.translatePath(xbmcaddon.Addon(id=ADDON_ID).getAddonInfo('profile'))
    if not xbmcvfs.exists(prof):
        xbmcvfs.mkdirs(prof)
    return os.path.join(prof, 'exclusions.json')


def _load_exclusions():
    try:
        path = _exclusions_path()
        if not xbmcvfs.exists(path):
            return []
        f = xbmcvfs.File(path)
        try:
            value = json.loads(f.read() or '[]')
        finally:
            f.close()
    except (OSError, RuntimeError, ValueError) as exc:
        xbmc.log('[service.scrob] could not load exclusions: {}'.format(exc), xbmc.LOGWARNING)
        return []
    if not isinstance(value, list):
        xbmc.log('[service.scrob] exclusions.json must contain a list', xbmc.LOGWARNING)
        return []
    return [e for e in value if isinstance(e, str) and e.strip()]


def _norm_path(path):
    p = (path or '').strip().replace('\\', '/').lower()
    # Credentials embedded in URL-style paths (smb://user:pass@host/...) must not affect matching.
    m = re.match(r'^([a-z][a-z0-9+.-]*://)(?:[^/@]*@)?(.*)$', p)
    if m:
        p = m.group(1) + m.group(2)
    return p.rstrip('/')


def _path_excluded(file_path, rules):
    """True when file_path equals, or sits beneath, any normalised rule. A
    stack:// path is excluded if any of its parts is."""
    if not file_path:
        return False
    if file_path.startswith('stack://'):
        parts = file_path[len('stack://'):].split(' , ')
    else:
        parts = [file_path]
    for part in parts:
        p = _norm_path(part)
        for r in rules:
            if p == r or p.startswith(r + '/') or (
                    r.startswith('plugin://') and p.startswith(r + '?')):
                return True
    return False


def _is_excluded(item):
    rules = [r for r in (_norm_path(p) for p in _load_exclusions()) if r]
    item = item or {}
    paths = [item.get('_file')] + item.get('_source_paths', [])
    return bool(rules) and any(_path_excluded(path, rules) for path in paths)


def _playback_source_paths(player):
    paths = []
    # Kodi 20+ exposes the original plugin item even after stream resolution.
    if hasattr(player, 'getPlayingItem'):
        try:
            playing_item = player.getPlayingItem()
            paths.extend([playing_item.getProperty('original_listitem_url'),
                          playing_item.getPath()])
        except RuntimeError as exc:
            xbmc.log('[service.scrob] could not read playback source: {}'.format(exc), xbmc.LOGDEBUG)
    else:
        paths.append(xbmc.getInfoLabel('Player.Property(original_listitem_url)'))
    return list(dict.fromkeys(path for path in paths if path))


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
        xbmc.log('[service.scrob] request skipped: Scrob URL is empty', xbmc.LOGWARNING)
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


def _exclusions_summary_text():
    n = len(_load_exclusions())
    if not n:
        return 'None - everything is scrobbled'
    return '{} excluded path{}'.format(n, '' if n == 1 else 's')


def _update_exclusions_summary():
    try:
        a = xbmcaddon.Addon(id=ADDON_ID)
        text = _exclusions_summary_text()
        if a.getSetting('excluded_summary') != text:
            a.setSetting('excluded_summary', text)
    except Exception:
        pass


def _save_exclusions(paths):
    f = xbmcvfs.File(_exclusions_path(), 'w')
    try:
        f.write(json.dumps(paths))
    finally:
        f.close()
    _update_exclusions_summary()


def _expand_source(path):
    # A Kodi source with several folders is a multipath:// of url-encoded entries.
    if path.startswith('multipath://'):
        return [unquote(p) for p in path[len('multipath://'):].split('/') if p]
    return [path]


def _pick_kodi_source():
    dialog = xbmcgui.Dialog()
    try:
        sources = _kodi_rpc('Files.GetSources', {'media': 'video'}).get('sources', [])
    except Exception:
        sources = []
    sources = [s for s in sources if s.get('file')]
    if not sources:
        dialog.notification('Scrob', 'No video sources found', xbmcgui.NOTIFICATION_INFO)
        return []
    idx = dialog.select('Kodi video sources', [u'{}  -  {}'.format(s.get('label', ''), s['file']) for s in sources])
    return _expand_source(sources[idx]['file']) if idx >= 0 else []


def _pick_video_addon():
    dialog = xbmcgui.Dialog()
    addon = xbmcaddon.Addon(id=ADDON_ID)
    try:
        addons = _kodi_rpc('Addons.GetAddons', {
            'type': 'xbmc.addon.video', 'enabled': True,
            'properties': ['name'],
        }).get('addons', [])
    except (RuntimeError, ValueError) as exc:
        xbmc.log('[service.scrob] could not list video add-ons: {}'.format(exc), xbmc.LOGWARNING)
        dialog.notification('Scrob', addon.getLocalizedString(30028), xbmcgui.NOTIFICATION_ERROR)
        return []
    if not addons:
        dialog.notification('Scrob', addon.getLocalizedString(30029), xbmcgui.NOTIFICATION_INFO)
        return []
    addons.sort(key=lambda a: (a.get('name') or a['addonid']).lower())
    idx = dialog.select(addon.getLocalizedString(30027), [
        '{} ({})'.format(a.get('name') or a['addonid'], a['addonid']) for a in addons])
    return ['plugin://{}/'.format(addons[idx]['addonid'])] if idx >= 0 else []


def _manage_exclusions():
    dialog = xbmcgui.Dialog()
    try:
        while True:
            paths = _load_exclusions()
            entries = [('[Add folder...]', 'folder', None),
                       ('[Add from Kodi sources...]', 'source', None),
                       (xbmcaddon.Addon(id=ADDON_ID).getLocalizedString(30026), 'addon', None)]
            entries += [(p, 'remove', p) for p in paths]
            if paths:
                entries.append(('[Clear all]', 'clear', None))
            entries.append(('[Done]', 'done', None))

            idx = dialog.select('Scrob - excluded paths (select an entry to remove it)',
                                [e[0] for e in entries])
            if idx < 0 or entries[idx][1] == 'done':
                break
            _, kind, value = entries[idx]

            if kind in ('folder', 'source', 'addon'):
                if kind == 'folder':
                    picked = dialog.browseSingle(0, 'Folder to exclude from scrobbling', 'video')
                    new = [picked] if picked else []
                elif kind == 'source':
                    new = _pick_kodi_source()
                else:
                    new = _pick_video_addon()
                known = set(_norm_path(p) for p in paths)
                for p in new:
                    if _norm_path(p) and _norm_path(p) not in known:
                        paths.append(p)
                        known.add(_norm_path(p))
                _save_exclusions(paths)
            elif kind == 'clear':
                if dialog.yesno('Scrob', 'Remove all excluded paths?'):
                    _save_exclusions([])
            elif kind == 'remove':
                if dialog.yesno('Scrob', u'Stop excluding:\n{}'.format(value)):
                    paths.remove(value)
                    _save_exclusions(paths)
    finally:
        _update_exclusions_summary()
    n = len(_load_exclusions())
    dialog.notification('Scrob', u'{} excluded path{} saved'.format(n, '' if n == 1 else 's'),
                        xbmcgui.NOTIFICATION_INFO)


# ── Scrobbling ────────────────────────────────────────────────────────────────

def _post(method, item, player_state, ended=False, source=None):
    report_start = method == 'Player.OnPlay'
    if not _settings()['sync_to_scrob']:
        if report_start:
            xbmc.log('[service.scrob] playback skipped: Send events to Scrob is disabled', xbmc.LOGINFO)
        return
    if _is_excluded(item):
        if report_start:
            xbmc.log('[service.scrob] playback skipped: a saved exclusion matches', xbmc.LOGINFO)
        return
    if report_start:
        paths = [item.get('_file', '')] + item.get('_source_paths', [])
        has_plugin_source = any(path.lower().startswith('plugin://') for path in paths if path)
        xbmc.log('[service.scrob] sending playback: type={}, plugin_source={}'.format(
            item['type'], has_plugin_source), xbmc.LOGINFO)
    # Keys starting with '_' are add-on-local (e.g. artwork for the rating
    # popup) and are not part of what Scrob is sent.
    payload = {'method': method,
               'item': {k: v for k, v in item.items() if not k.startswith('_')},
               'player': player_state}
    if source:
        # Lets Scrob tell a library "mark as watched" apart from real playback.
        payload['source'] = source
    if method == 'Player.OnStop':
        payload['params'] = {'data': {'end': ended}}
    reply = _api('webhooks/kodi', payload)
    if report_start:
        xbmc.log('[service.scrob] playback request {}'.format(
            'received a response' if reply is not None else 'failed; see request warnings'),
            xbmc.LOGINFO if reply is not None else xbmc.LOGWARNING)
    if reply is not None:
        xbmc.log(u'[service.scrob] {} {} -> {}'.format(
            method, json.dumps(payload['item'], ensure_ascii=False),
            json.dumps(reply, ensure_ascii=False)), xbmc.LOGDEBUG)


def _post_rating(item, rating):
    if _is_excluded(item):
        return
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


def _pick_art(art, media_type):
    """(poster, fanart) URLs for the rating popup from a Kodi art dict. An
    episode borrows its show's poster/fanart - an episode still is a poor fit
    for a portrait slot."""
    art = art or {}
    if media_type == 'episode':
        poster = art.get('tvshow.poster') or art.get('season.poster') or art.get('poster') or art.get('thumb')
        fanart = art.get('tvshow.fanart') or art.get('fanart')
    else:
        poster = art.get('poster') or art.get('thumb')
        fanart = art.get('fanart')
    return {'poster': poster or '', 'fanart': fanart or ''}


class _RatingDialog(xbmcgui.WindowXMLDialog):
    """Landscape rating popup: a row of ten stars that fill up to the focused
    one, with the number shown large underneath. Left/Right (or the mouse)
    picks, OK confirms, Back skips. Layout lives in
    resources/skins/Default/720p/script-scrob-rating.xml."""

    _FIRST_STAR_ID = 101
    _ACTIONS_CLOSE = (9, 10, 13, 92)  # PARENT_DIR, PREVIOUS_MENU, STOP, NAV_BACK

    title = ''
    default = 5
    result = None
    art = None

    def onInit(self):
        self.setProperty('title', self.title)
        self.setProperty('poster', (self.art or {}).get('poster', ''))
        self.setProperty('fanart', (self.art or {}).get('fanart', ''))
        self.setProperty('rating', str(self.default))
        self.setFocusId(self._FIRST_STAR_ID - 1 + self.default)

    def onFocus(self, control_id):
        value = control_id - (self._FIRST_STAR_ID - 1)
        if 1 <= value <= 10:
            self.setProperty('rating', str(value))

    def onClick(self, control_id):
        value = control_id - (self._FIRST_STAR_ID - 1)
        if 1 <= value <= 10:
            self.result = value
            self.close()

    def onAction(self, action):
        if action.getId() in self._ACTIONS_CLOSE:
            self.close()


def _pick_rating(label, default, art=None):
    """Show the rating popup; returns 1-10, or None when skipped."""
    try:
        addon_path = xbmcaddon.Addon(id=ADDON_ID).getAddonInfo('path')
        dlg = _RatingDialog('script-scrob-rating.xml', addon_path, 'Default', '720p')
        dlg.title = label
        dlg.default = default
        dlg.art = art
        dlg.doModal()
        result = dlg.result
        del dlg
        return result
    except Exception as exc:
        # Skin XML missing/rejected on some odd Kodi build: fall back to the
        # plain list so rating still works.
        xbmc.log('[service.scrob] rating popup failed, using list: {}'.format(exc), xbmc.LOGWARNING)
    options = ['{0} {1}'.format(i, u'\u2605' * i) for i in range(1, 11)]
    idx = xbmcgui.Dialog().select(label, options, preselect=default - 1)
    return idx + 1 if idx >= 0 else None


def _ask_and_post_rating(item):
    xbmc.sleep(1500)
    if _is_excluded(item):
        return
    if item['type'] == 'episode':
        label = '{} S{:02d}E{:02d}'.format(
            item.get('showtitle', ''), item.get('season', 0), item.get('episode', 0))
    else:
        label = item.get('title', '')
    rating = _pick_rating('Rate: {}'.format(label), _settings()['rating_default'], item.get('_art'))
    if rating:
        _post_rating(item, rating)


def _kodi_rpc(method, params=None):
    query = json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params or {}, 'id': 1})
    response = json.loads(xbmc.executeJSONRPC(query))
    if response.get('error'):
        xbmc.log('[service.scrob] Kodi RPC {} failed: {}'.format(
            method, response['error'].get('message', 'Unknown error')), xbmc.LOGWARNING)
    return response.get('result', {})


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

    def _expect(lib_type, lib_id, playcount):
        # Kodi delivers the resulting VideoLibrary.OnUpdate notifications
        # asynchronously - possibly long after the time-based mute above has
        # expired on a big library - so each write is also remembered by id
        # and its echo is recognised and dropped on arrival.
        if monitor is not None:
            monitor._expect_echo(lib_type, lib_id, playcount)

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
            # Only ever fill a title Kodi has as unwatched. Comparing counts
            # instead let every stray extra play recorded in Scrob push Kodi's
            # count up again, which is what kept the sync looping.
            if scrob_movies.get(tmdb_id, 0) > 0 and not km.get('playcount', 0):
                changes['playcount'] = scrob_movies[tmdb_id]
            want = _want_rating(scrob_movie_ratings.get(tmdb_id))
            if want >= 1 and not km.get('userrating', 0):
                changes['userrating'] = want
            if changes:
                changes['movieid'] = km['movieid']
                if 'playcount' in changes:
                    _expect('movie', km['movieid'], changes['playcount'])
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
            if scrob_eps.get(k, 0) > 0 and not ke.get('playcount', 0):
                changes['playcount'] = scrob_eps[k]
            want = _want_rating(scrob_ep_ratings.get(k))
            if want >= 1 and not ke.get('userrating', 0):
                changes['userrating'] = want
            if changes:
                changes['episodeid'] = ke['episodeid']
                if 'playcount' in changes:
                    _expect('episode', ke['episodeid'], changes['playcount'])
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


_show_uid_cache = {}


def _show_uniqueid(tvshowid):
    """The TV show's own uniqueid. Scrob resolves an episode's series from
    show-level ids; an episode's tvdb/imdb id never matches a series."""
    if not isinstance(tvshowid, int) or tvshowid < 0:
        return {}
    uid = _show_uid_cache.get(tvshowid)
    if uid is None:
        try:
            details = _kodi_rpc('VideoLibrary.GetTVShowDetails', {
                'tvshowid': tvshowid, 'properties': ['uniqueid']}).get('tvshowdetails') or {}
        except Exception:
            details = {}
        uid = details.get('uniqueid') or {}
        if uid:
            _show_uid_cache[tvshowid] = uid
    return uid


def _read_item(player, report_unsupported=False):
    """Read the current player item via JSON-RPC and position. Returns (item, player_state) or (None, None)."""
    try:
        result = _kodi_rpc('Player.GetItem', {
            'playerid': 1,
            'properties': ['title', 'showtitle', 'season', 'episode', 'year', 'uniqueid', 'art', 'file',
                           'tvshowid'],
        })
        kodi_item = result.get('item', {})
    except (RuntimeError, ValueError) as exc:
        xbmc.log('[service.scrob] could not read player metadata: {}'.format(exc), xbmc.LOGWARNING)
        return None, None

    media_type = kodi_item.get('type', '')
    if media_type not in ('movie', 'episode'):
        xbmc.log('[service.scrob] playback ignored: Kodi media type={!r}; '
                 'only movie and episode are supported; showtitle_present={}, '
                 'season={}, episode={}'.format(
                     media_type, bool(kodi_item.get('showtitle')),
                     kodi_item.get('season'), kodi_item.get('episode')),
                 xbmc.LOGINFO if report_unsupported else xbmc.LOGDEBUG)
        return None, None

    uid = kodi_item.get('uniqueid') or {}
    if media_type == 'episode':
        uid = _show_uniqueid(kodi_item.get('tvshowid')) or uid
    item = {'type': media_type, 'uniqueid': uid, '_art': _pick_art(kodi_item.get('art'), media_type),
            '_file': kodi_item.get('file', ''), '_source_paths': _playback_source_paths(player)}
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
        # (lib_type, lib_id) -> playcount the add-on itself wrote during a sync
        self._echoes = {}
        # timestamps of recent mark-as-watched scrobbles, for the burst breaker
        self._update_times = []
        self._burst_until = 0

    def _expect_echo(self, lib_type, lib_id, playcount):
        with self._lock:
            self._echoes[(lib_type, lib_id)] = int(playcount or 0)

    def _consume_echo(self, lib_type, lib_id, playcount):
        """True when this OnUpdate is the notification for a playcount the
        add-on wrote itself (and forget it)."""
        with self._lock:
            expected = self._echoes.get((lib_type, lib_id))
            if expected is None or int(playcount or 0) != expected:
                return False
            del self._echoes[(lib_type, lib_id)]
            return True

    _BURST_LIMIT = 30      # mark-as-watched scrobbles ...
    _BURST_WINDOW = 60     # ... within this many seconds is a bulk operation
    _BURST_PAUSE = 600     # ... so stop scrobbling them for this long

    def _burst_tripped(self):
        """A person marking things watched does a handful at a time; a library
        scan, sync or another add-on touching hundreds of rows in a minute is
        not something to replay into Scrob as fresh plays."""
        now = time.time()
        if now < self._burst_until:
            return True
        with self._lock:
            self._update_times = [t for t in self._update_times if now - t < self._BURST_WINDOW]
            self._update_times.append(now)
            if len(self._update_times) > self._BURST_LIMIT:
                self._burst_until = now + self._BURST_PAUSE
                self._update_times = []
                xbmc.log('[service.scrob] Too many library updates at once - not scrobbling '
                         'mark-as-watched for {} minutes'.format(self._BURST_PAUSE // 60), xbmc.LOGWARNING)
                return True
        return False

    def _cache(self, item, ps):
        with self._lock:
            if item and self._media_key(item) == self._media_key(self._last_item):
                item['_source_paths'] = list(dict.fromkeys(
                    self._last_item.get('_source_paths', []) +
                    [self._last_item.get('_file', '')] + item.get('_source_paths', [])))
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
                'properties': ['title', 'year', 'uniqueid', 'runtime', 'file'],
            }).get('moviedetails') or {}
            if not d:
                return None, None
            item = {'type': 'movie', 'id': lib_id, 'uniqueid': d.get('uniqueid') or {},
                    'title': d.get('title', ''), 'year': d.get('year'), '_file': d.get('file', '')}
            runtime = int(d.get('runtime') or 0) or 3600
        else:
            d = _kodi_rpc('VideoLibrary.GetEpisodeDetails', {
                'episodeid': lib_id,
                'properties': ['title', 'showtitle', 'season', 'episode', 'uniqueid', 'runtime', 'file',
                               'tvshowid'],
            }).get('episodedetails') or {}
            if not d:
                return None, None
            item = {'type': 'episode', 'id': lib_id,
                    'uniqueid': _show_uniqueid(d.get('tvshowid')) or d.get('uniqueid') or {},
                    'title': d.get('title', ''), 'showtitle': d.get('showtitle', ''),
                    'season': d.get('season', 0), 'episode': d.get('episode', 0),
                    '_file': d.get('file', '')}
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
        # The echo of our own sync writing this playcount, not a user action.
        if self._consume_echo(lib_type, lib_id, d.get('playcount')):
            return
        if self._burst_tripped():
            return

        item, ps = self._library_item(lib_type, lib_id)
        cached_item, _ = self._cached()
        if item and cached_item and self._media_key(item) == self._media_key(cached_item):
            item['_source_paths'] = cached_item.get('_source_paths', [])
        if not item or _is_excluded(item):
            return
        # Already scrobbled as complete by the playback's own OnStop (or a
        # previous OnUpdate for the same title) — nothing to add.
        if self._recently_scrobbled(item):
            return

        xbmc.log('[service.scrob] Scrobbling mark-as-watched: {}'.format(
            self._media_key(item)), xbmc.LOGINFO)
        _post('Player.OnStop', item, ps, ended=True, source='library_update')
        self._mark_scrobbled(item)

    def onSettingsChanged(self):
        _update_connection_status()
        _update_exclusions_summary()

    def onNotification(self, sender, method, data):
        if method == 'Player.OnPlay':
            self._stop_progress()
            self._cache(None, None)
            item, ps = None, None
            for attempt in range(6):
                xbmc.sleep(500)
                item, ps = _read_item(self._player, report_unsupported=attempt == 5)
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
                    # Also suppress late library echoes for excluded playback.
                    self._mark_scrobbled(item)
                    if not _is_excluded(item) and (
                            (item['type'] == 'movie' and s['rate_on_completion']) or
                            (item['type'] == 'episode' and s['rate_episodes_on_completion'])):
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
    xbmc.log('[service.scrob] Playback decision diagnostics enabled', xbmc.LOGINFO)
    _update_connection_status()
    _update_exclusions_summary()
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
if __name__ == '__main__':
    _ACTION = sys.argv[1] if len(sys.argv) > 1 else ''
    if _ACTION == 'authorize':
        _device_authorize()
    elif _ACTION == 'signout':
        _forget_authorization()
    elif _ACTION == 'exclusions':
        _manage_exclusions()
    else:
        run()
