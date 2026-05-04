import json
import threading
import xbmc
import xbmcaddon

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
    }


def _post(method, item, player_state, ended=False):
    s = _settings()
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


def _hms(secs):
    secs = max(0, int(secs or 0))
    return {'hours': secs // 3600, 'minutes': (secs % 3600) // 60, 'seconds': secs % 60}


def _read_item(player):
    """Read the current VideoInfoTag and player position. Returns (item, player_state) or (None, None)."""
    try:
        tag = player.getVideoInfoTag()
    except Exception:
        return None, None

    media_type = tag.getMediaType()
    if media_type not in ('movie', 'episode'):
        return None, None

    uid = {}
    tmdb = tag.getUniqueID('tmdb')
    tvdb = tag.getUniqueID('tvdb')
    imdb = tag.getIMDBNumber()
    if tmdb: uid['tmdb'] = tmdb
    if tvdb: uid['tvdb'] = tvdb
    if imdb: uid['imdb'] = imdb

    item = {'type': media_type, 'uniqueid': uid}
    if media_type == 'movie':
        item['title'] = tag.getTitle()
        item['year'] = tag.getYear()
    else:
        item['title'] = tag.getEpisodeTitle() or tag.getTitle()
        item['showtitle'] = tag.getTVShowTitle()
        item['season'] = tag.getSeason()
        item['episode'] = tag.getEpisode()

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
            # Brief delay so Kodi populates VideoInfoTag before we read it
            xbmc.sleep(500)
            item, ps = _read_item(self._player)
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
    while not monitor.abortRequested():
        monitor.waitForAbort(1)
    monitor._stop_progress()
    xbmc.log('[service.scrob] Stopped', xbmc.LOGINFO)


run()
