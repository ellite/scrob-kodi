import json
import threading
import xbmc
import xbmcaddon
import xbmcgui

try:
    from urllib.request import Request, urlopen
    from urllib.parse import quote
except ImportError:
    from urllib2 import Request, urlopen
    from urllib import quote

ADDON_ID = 'service.scrob'


def _settings():
    a = xbmcaddon.Addon(id=ADDON_ID)
    return {
        'url': a.getSetting('scrob_url').rstrip('/'),
        'key': a.getSetting('api_key').strip(),
        'interval': max(10, int(a.getSetting('progress_interval') or 60)),
        'sync_to_scrob': a.getSettingBool('sync_to_scrob'),
        'sync_from_scrob': a.getSettingBool('sync_from_scrob'),
        'rate_on_completion': a.getSettingBool('rate_on_completion'),
        'rate_episodes_on_completion': a.getSettingBool('rate_episodes_on_completion'),
    }


def _post(method, item, player_state, ended=False):
    s = _settings()
    if not s['sync_to_scrob']:
        return
    if not s['key']:
        xbmc.log('[service.scrob] API key not configured — skipping', xbmc.LOGWARNING)
        return
    payload = {'method': method, 'item': item, 'player': player_state}
    if method == 'Player.OnStop':
        payload['params'] = {'data': {'end': ended}}
    url = '{}/api/proxy/webhooks/kodi?api_key={}'.format(s['url'], quote(s['key'], safe=''))
    try:
        data = json.dumps(payload).encode('utf-8')
        req = Request(url, data=data, headers={'Content-Type': 'application/json'})
        urlopen(req, timeout=10)
        xbmc.log('[service.scrob] {}'.format(method), xbmc.LOGDEBUG)
    except Exception as exc:
        xbmc.log('[service.scrob] POST failed: {}'.format(exc), xbmc.LOGWARNING)


def _post_rating(item, rating):
    s = _settings()
    if not s['key']:
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
    url = '{}/api/proxy/webhooks/kodi/rating?api_key={}'.format(s['url'], quote(s['key'], safe=''))
    try:
        data = json.dumps(payload).encode('utf-8')
        req = Request(url, data=data, headers={'Content-Type': 'application/json'})
        urlopen(req, timeout=10)
        xbmc.log('[service.scrob] Rating submitted: {}'.format(rating), xbmc.LOGDEBUG)
    except Exception as exc:
        xbmc.log('[service.scrob] Rating POST failed: {}'.format(exc), xbmc.LOGWARNING)


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


def _sync_from_scrob():
    s = _settings()
    if not s['key']:
        xbmc.log('[service.scrob] API key not configured — skipping sync', xbmc.LOGWARNING)
        return
    url = '{}/api/proxy/webhooks/kodi/history?api_key={}'.format(s['url'], quote(s['key'], safe=''))
    try:
        resp = urlopen(Request(url), timeout=30)
        library = json.loads(resp.read().decode('utf-8'))
    except Exception as exc:
        xbmc.log('[service.scrob] Sync from Scrob failed: {}'.format(exc), xbmc.LOGWARNING)
        return

    # Movies — match by TMDB ID
    scrob_movies = {str(m['tmdb_id']): m['play_count'] for m in library.get('movies', [])}
    if scrob_movies:
        kodi_movies = _kodi_rpc('VideoLibrary.GetMovies',
                                {'properties': ['uniqueid', 'playcount']}).get('movies', [])
        for km in kodi_movies:
            tmdb_id = str(km.get('uniqueid', {}).get('tmdb', '') or '')
            if not tmdb_id:
                continue
            scrob_count = scrob_movies.get(tmdb_id, 0)
            if scrob_count > km['playcount']:
                _kodi_rpc('VideoLibrary.SetMovieDetails',
                          {'movieid': km['movieid'], 'playcount': scrob_count})

    # Episodes — match by show TMDB ID + season + episode number
    scrob_eps = {}
    for e in library.get('episodes', []):
        key = (str(e['show_tmdb_id']), e['season_number'], e['episode_number'])
        scrob_eps[key] = e['play_count']

    if scrob_eps:
        kodi_shows = _kodi_rpc('VideoLibrary.GetTVShows',
                               {'properties': ['uniqueid']}).get('tvshows', [])
        show_map = {}
        for ks in kodi_shows:
            tmdb_id = str(ks.get('uniqueid', {}).get('tmdb', '') or '')
            if tmdb_id:
                show_map[tmdb_id] = ks['tvshowid']

        kodi_eps = _kodi_rpc('VideoLibrary.GetEpisodes',
                             {'properties': ['tvshowid', 'season', 'episode', 'playcount']}).get('episodes', [])
        kodi_ep_map = {(ke['tvshowid'], ke['season'], ke['episode']): ke for ke in kodi_eps}

        for (show_tmdb, season, episode), scrob_count in scrob_eps.items():
            tvshowid = show_map.get(show_tmdb)
            if not tvshowid:
                continue
            ke = kodi_ep_map.get((tvshowid, season, episode))
            if ke and scrob_count > ke['playcount']:
                _kodi_rpc('VideoLibrary.SetEpisodeDetails',
                          {'episodeid': ke['episodeid'], 'playcount': scrob_count})

    xbmc.log('[service.scrob] Library sync from Scrob complete', xbmc.LOGINFO)


def _hms(secs):
    secs = max(0, int(secs or 0))
    return {'hours': secs // 3600, 'minutes': (secs % 3600) // 60, 'seconds': secs % 60}


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
    def __init__(self, player, interval):
        super(_ProgressThread, self).__init__(daemon=True)
        self._player = player
        self._interval = interval
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self._interval):
            if not self._player.isPlayingVideo():
                break
            item, ps = _read_item(self._player)
            if item:
                _post('Player.OnAVChange', item, ps)


class ScrobMonitor(xbmc.Monitor):
    def __init__(self):
        super(ScrobMonitor, self).__init__()
        self._player = xbmc.Player()
        self._lock = threading.Lock()
        self._thread = None
        self._last_item = None
        self._last_ps = None

    def _cache(self, item, ps):
        with self._lock:
            self._last_item = item
            self._last_ps = ps

    def _cached(self):
        with self._lock:
            return self._last_item, self._last_ps

    def _start_progress(self):
        self._stop_progress()
        s = _settings()
        self._thread = _ProgressThread(self._player, s['interval'])
        self._thread.start()

    def _stop_progress(self):
        if self._thread:
            self._thread.stop()
            self._thread = None

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
                _post('Player.OnStop', item, ps, ended=ended)
                if ended:
                    s = _settings()
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


def run():
    xbmc.log('[service.scrob] Starting', xbmc.LOGINFO)
    monitor = ScrobMonitor()
    if _settings()['sync_from_scrob']:
        _sync_from_scrob()
    while not monitor.abortRequested():
        monitor.waitForAbort(1)
    monitor._stop_progress()
    xbmc.log('[service.scrob] Stopped', xbmc.LOGINFO)


run()
