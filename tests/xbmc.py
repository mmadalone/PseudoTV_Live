# -*- coding: utf-8 -*-
# Copyright: (c) 2019, Dag Wieers (@dagwieers) <dag@wieers.com>
# GNU General Public License v3.0 (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
"""This file implements the Kodi xbmc module, either using stubs or alternative functionality"""

# pylint: disable=invalid-name,no-self-use,too-many-branches,unused-argument

import os
import json
import time
import weakref
from xbmcextra import ADDON_ID, global_settings, import_language

LOGLEVELS = ['Debug', 'Info', 'Notice', 'Warning', 'Error', 'Severe', 'Fatal', 'None']
LOGDEBUG = 0
LOGINFO = 1
LOGNOTICE = 2
LOGWARNING = 3
LOGERROR = 4
LOGSEVERE = 5
LOGFATAL = 6
LOGNONE = 7

INFO_LABELS = {
    'Container.FolderPath': 'plugin://' + ADDON_ID + '/',
    'System.BuildVersion': '18.2',
    # === Additions for PseudoTV Live ===
    # `ScreenResolution` is read at module-level by overlay.py / overlaytool.py
    # via Builtin.getResolution() which expects "WxH - mode" format.
    # PseudoTV's Builtin.getInfoLabel wrapper namespaces keys as 'PARAM.KEY'
    # (default param='ListItem'); module-level overlay.py:26 / overlaytool.py:26
    # do `BUILTIN.getResolution()` which queries 'System.ScreenResolution' and
    # expects "WxH - mode" format.
    'System.ScreenResolution': '1920x1080 - 1080p',
}

REGIONS = {
    'datelong': '%A, %e %B %Y',
    'dateshort': '%Y-%m-%d',
}

settings = global_settings()
LANGUAGE = import_language(language=settings.get('locale.language'))


class Keyboard(object):  # pylint: disable=useless-object-inheritance
    """A stub implementation of the xbmc Keyboard class"""

    def __init__(self, line='', heading=''):
        """A stub constructor for the xbmc Keyboard class"""

    def doModal(self, autoclose=0):
        """A stub implementation for the xbmc Keyboard class doModal() method"""

    def isConfirmed(self):
        """A stub implementation for the xbmc Keyboard class isConfirmed() method"""
        return True

    def getText(self):
        """A stub implementation for the xbmc Keyboard class getText() method"""
        return 'test'


class Monitor(object):  # pylint: disable=useless-object-inheritance
    """A stub implementation of the xbmc Monitor class"""
    _instances = set()

    def __init__(self, line='', heading=''):
        """A stub constructor for the xbmc Monitor class"""
        self.iteration = 0
        self._instances.add(weakref.ref(self))

    def abortRequested(self):
        """A stub implementation for the xbmc Keyboard class abortRequested() method"""
        self.iteration += 1
        print('Iteration: %s' % self.iteration)
        return self.iteration % 5 == 0

    def waitForAbort(self, timeout=None):
        """A stub implementation for the xbmc Monitor class waitForAbort() method"""
        try:
            time.sleep(timeout)
        except KeyboardInterrupt:
            return True
        except Exception:  # pylint: disable=broad-except
            return True
        return False

    @classmethod
    def getinstances(cls):
        """Return the instances for this class"""
        dead = set()
        for ref in cls._instances:
            obj = ref()
            if obj is not None:
                yield obj
            else:
                dead.add(ref)
        cls._instances -= dead


class Player(object):  # pylint: disable=useless-object-inheritance
    """A stub implementation of the xbmc Player class"""
    def __init__(self):
        """A stub constructor for the xbmc Player class"""
        self._count = 0

    def play(self, item='', listitem=None, windowed=False, startpos=-1):
        """A stub implementation for the xbmc Player class play() method"""
        return

    def stop(self):
        """A stub implementation for the xbmc Player class stop() method"""
        return

    def getPlayingFile(self):
        """A stub implementation for the xbmc Player class getPlayingFile() method"""
        return '/foo/bar'

    def isPlaying(self):
        """A stub implementation for the xbmc Player class isPlaying() method"""
        # Return True four times out of five
        self._count += 1
        return bool(self._count % 5 != 0)

    def seekTime(self, seekTime):
        """A stub implementation for the xbmc Player class seekTime() method"""
        return

    def showSubtitles(self, bVisible):
        """A stub implementation for the xbmc Player class showSubtitles() method"""
        return

    def getTotalTime(self):
        """A stub implementation for the xbmc Player class getTotalTime() method"""
        return 0

    def getTime(self):
        """A stub implementation for the xbmc Player class getTime() method"""
        return 0

    def getVideoInfoTag(self):
        """A stub implementation for the xbmc Player class getVideoInfoTag() method"""
        return VideoInfoTag()


class PlayList(object):  # pylint: disable=useless-object-inheritance
    """A stub implementation of the xbmc PlayList class"""

    def __init__(self, playList):
        """A stub constructor for the xbmc PlayList class"""

    def getposition(self):
        """A stub implementation for the xbmc PlayList class getposition() method"""
        return 0

    def add(self, url, listitem=None, index=-1):
        """A stub implementation for the xbmc PlayList class add() method"""

    def size(self):
        """A stub implementation for the xbmc PlayList class size() method"""


class VideoInfoTag(object):  # pylint: disable=useless-object-inheritance
    """A stub implementation of the xbmc VideoInfoTag class"""

    def __init__(self):
        """A stub constructor for the xbmc VideoInfoTag class"""

    def getSeason(self):
        """A stub implementation for the xbmc VideoInfoTag class getSeason() method"""
        return 0

    def getEpisode(self):
        """A stub implementation for the xbmc VideoInfoTag class getEpisode() method"""
        return 0

    def getTVShowTitle(self):
        """A stub implementation for the xbmc VideoInfoTag class getTVShowTitle() method"""
        return ''

    def getPlayCount(self):
        """A stub implementation for the xbmc VideoInfoTag class getPlayCount() method"""
        return 0

    def getRating(self):
        """A stub implementation for the xbmc VideoInfoTag class getRating() method"""
        return 0


def executebuiltin(string, wait=False):  # pylint: disable=unused-argument
    """A stub implementation of the xbmc executebuiltin() function"""
    assert isinstance(string, str)
    assert isinstance(wait, bool)


def executeJSONRPC(jsonrpccommand):
    """A reimplementation of the xbmc executeJSONRPC() function"""
    assert isinstance(jsonrpccommand, str)
    command = json.loads(jsonrpccommand)

    # Handle a list of commands sequentially
    if isinstance(command, list):
        ret = []
        for action in command:
            ret.append(executeJSONRPC(json.dumps(action)))
        return json.dumps(ret)

    ret = {'id': command.get('id'), 'jsonrpc': '2.0', 'result': 'OK'}
    if command.get('method').startswith('Input'):
        pass
    elif command.get('method') == 'Player.Open':
        pass
    elif command.get('method') == 'Settings.GetSettingValue':
        key = command.get('params').get('setting')
        ret.update(result={'value': settings.get(key)})
    elif command.get('method') == 'Addons.GetAddonDetails':
        if command.get('params', {}).get('addonid') == 'script.module.inputstreamhelper':
            ret.update(result={'addon': {'enabled': 'true', 'version': '0.3.5'}})
        else:
            ret.update(result={'addon': {'enabled': 'true', 'version': '1.2.3'}})
    elif command.get('method') == 'Textures.GetTextures':
        ret.update(result={'textures': [{'cachedurl': '', 'imagehash': '', 'lasthashcheck': '', 'textureid': 4837, 'url': ''}]})
    elif command.get('method') == 'Textures.RemoveTexture':
        pass
    elif command.get('method') == 'JSONRPC.NotifyAll':
        # Send a notification to all instances of subclasses
        for sub in Monitor.__subclasses__():
            for obj in sub.getinstances():
                obj.onNotification(
                    sender=command.get('params').get('sender'),
                    method=command.get('params').get('message'),
                    data=json.dumps(command.get('params').get('data')),
                )
    else:
        log("executeJSONRPC does not implement method '{method}'".format(**command), LOGERROR)
        return json.dumps({'error': {'code': -1, 'message': 'Not implemented'}, 'id': command.get('id'), 'jsonrpc': '2.0'})
    return json.dumps(ret)


def getCondVisibility(string):
    """A reimplementation of the xbmc getCondVisibility() function"""
    assert isinstance(string, str)
    if string == 'system.platform.android':
        return False
    if string.startswith('System.HasAddon'):
        return True
    return True


def getInfoLabel(key):
    """A reimplementation of the xbmc getInfoLabel() function"""
    assert isinstance(key, str)
    return INFO_LABELS.get(key)


def getLocalizedString(msgctxt):
    """A reimplementation of the xbmc getLocalizedString() function"""
    assert isinstance(msgctxt, int)
    for entry in LANGUAGE:
        if entry.msgctxt == '#%s' % msgctxt:
            return entry.msgstr or entry.msgid
    if int(msgctxt) >= 30000:
        log('Unable to translate #{msgctxt}'.format(msgctxt=msgctxt), LOGERROR)
    return '<Untranslated>'


def getRegion(key):
    """A reimplementation of the xbmc getRegion() function"""
    assert isinstance(key, str)
    return REGIONS.get(key)


def log(msg, level=0):
    """A reimplementation of the xbmc log() function"""
    assert isinstance(msg, str)
    assert isinstance(level, int)
    color1 = '\033[32;1m'
    color2 = '\033[32;0m'
    name = LOGLEVELS[level]
    if level in (4, 5, 6, 7):
        color1 = '\033[31;1m'
        if level in (6, 7):
            raise ValueError(msg)
    elif level in (2, 3):
        color1 = '\033[33;1m'
    elif level == 0:
        color2 = '\033[30;1m'
    print('{color1}{name}: {color2}{msg}\033[39;0m'.format(name=name, color1=color1, color2=color2, msg=msg))


def sleep(timemillis):
    """A reimplementation of the xbmc sleep() function"""
    assert isinstance(timemillis, int)
    time.sleep(timemillis / 1000)


# Language format constants — match Kodi's xbmc module
ISO_639_1    = 0
ISO_639_2    = 1
ENGLISH_NAME = 2


# madteevee: tiny stub for xbmc.convertLanguage. Real Kodi consults installed
# language packs; we hard-code a few common conversions sufficient for tests
# that exercise lang_codes.normalize_lang's xbmc-first path.
_CONVERT_LANG_2_TO_1 = {
    'eng': 'en', 'spa': 'es', 'fre': 'fr', 'fra': 'fr',
    'ger': 'de', 'deu': 'de', 'jpn': 'ja', 'cat': 'ca',
    'ita': 'it', 'por': 'pt', 'kor': 'ko', 'chi': 'zh',
    'rus': 'ru', 'ara': 'ar',
}
def convertLanguage(language, format=ISO_639_1):
    """Stub of xbmc.convertLanguage. Returns ISO 639-1 from 3-letter input,
    empty string when unknown (mirrors Kodi's behavior when language packs
    aren't installed — the bug lang_codes.normalize_lang's fallback handles)."""
    if not isinstance(language, str): return ''
    code = language.lower().strip()
    if format == ISO_639_1:
        if len(code) == 2: return code
        return _CONVERT_LANG_2_TO_1.get(code, '')
    return code


def translatePath(path):
    """A stub implementation of the xbmc translatePath() function"""
    assert isinstance(path, str)
    if path.startswith('special://home'):
        return path.replace('special://home', os.path.join(os.getcwd(), 'tests/'))
    if path.startswith('special://masterprofile'):
        return path.replace('special://masterprofile', os.path.join(os.getcwd(), 'tests/userdata/'))
    if path.startswith('special://profile'):
        return path.replace('special://profile', os.path.join(os.getcwd(), 'tests/userdata/'))
    if path.startswith('special://userdata'):
        return path.replace('special://userdata', os.path.join(os.getcwd(), 'tests/userdata/'))
    return path


# === Additions for PseudoTV Live (madteevee fork) ===
# Stubs for xbmc API surface that vrt.nu's mock doesn't cover but our addon uses.
# See `grep -rhoE "xbmc\\.[a-zA-Z_]+"` for the full reference list.

PLAYLIST_MUSIC = 0
PLAYLIST_VIDEO = 1


def getSupportedMedia(media):
    """Stub for xbmc.getSupportedMedia() — returns common Kodi-supported extensions."""
    assert isinstance(media, str)
    if media == 'video':
        return '.mkv|.mp4|.avi|.flv|.ts|.mov|.m4v|.webm|.mpg|.mpeg|.wmv|.iso|'
    if media == 'music':
        return '.mp3|.flac|.ogg|.wav|.m4a|.aac|.wma|'
    if media == 'picture':
        return '.jpg|.jpeg|.png|.gif|.bmp|.webp|'
    return ''


def executescript(script, *args):
    """Stub for xbmc.executescript() — no-op like executebuiltin."""
    assert isinstance(script, str)


def getGlobalIdleTime():
    """Stub for xbmc.getGlobalIdleTime() — returns 0 (just woke up)."""
    return 0


def getIPAddress():
    """Stub for xbmc.getIPAddress() — returns localhost."""
    return '127.0.0.1'


def playSFX(filename, useCached=False):
    """Stub for xbmc.playSFX() — no-op."""
    assert isinstance(filename, str)


class InfoTagVideo:
    """Stub for xbmc.InfoTagVideo class."""
    def __init__(self, offscreen=False):
        self.offscreen = offscreen

    def __getattr__(self, name):
        return lambda *a, **kw: None
