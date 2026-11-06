#   Copyright (C) 2025 Lunatixz
#
# This file is part of PseudoTV Live.
#
# PseudoTV Live is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PseudoTV Live is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with PseudoTV Live.  If not, see <http://www.gnu.org/licenses/>.

# -*- coding: utf-8 -*-

# madteevee (imports fork, imports.64): the "Up next" card, drawn on the video.
#
# Up next used to be an add-on dialog (overlay.OnNext, onnext.xml) opened with
# doModal() from the service's idle thread. Kodi treats every add-on dialog as
# modal, so while it was open (30 s visible, then minutes invisible) it took
# every key, Kodi refused to open menus and the guide, the RESTART pop-up could
# not appear, and the idle checks waited. Now overlay.Overlay draws it as
# controls on the full-screen video window (like the channel logo), which never
# take focus; this module holds every decision so the tests run the real code.
#
# Import-light pure module (same pattern as clock_helpers.py /
# restart_helpers.py): standard library only (ast, datetime, math, re), no Kodi
# imports, no add-on imports, no I/O, no clock reads (callers pass the facts),
# Python 3.8 syntax and APIs.
#
# Used by services.Player.__chkOnNext (when to show), overlay.Overlay (what and
# where to draw, when to hide) and overlay.Background (the end-of-show screen
# shares the time and the text).
import ast
import datetime
import math
import re

DEFAULT_TIME_FORMAT = '%H:%M'   # when Kodi's region format is empty or unusable
WAIT_STEP           = 0.25      # seconds between the wait thread's checks
CARD_W, CARD_H      = 240, 135  # artwork frame (onnext.xml)
CARD_BORDER         = 5         # onnext.xml bordersize
FRAME_IMAGE         = 'onnext_frame.png' # in the add-on's media folder: 240x135, 5 px white edges, see-through middle
FRAME_ASPECT        = 0         # ControlImage aspectRatio: stretch (the frame image is drawn at its own size)
ART_ASPECT          = 1         # ControlImage aspectRatio: scale (fills the inside, cropping the edges)
NAMES               = ('frame', 'art', 'shadow', 'text') # the controls, bottom to top
SETTLE_WAIT         = 0.15      # seconds for Kodi to draw a few frames of newly made controls before they show
BASE_W, BASE_H      = 1920, 1080 # the grid positions are given in (the Move tool's and the old window's)
TEXT_W, TEXT_H      = 960, 70   # text box (onnext.xml)
TEXT_GAP            = 140       # text top below the card top (onnext.xml posy)
SHADOW_OFFSET       = 1         # Kodi draws a font shadow 1 px right and down (GUIFont.cpp)
FRAME_COLOR         = '0xFF01416b'
TEXT_COLOR          = '0xFFFFFFFF'
SHADOW_COLOR        = '0xFF696969' # Kodi's 'dimgray'
TEXT_FONT           = 'font27'     # unknown fonts fall back to font13, as the old window did
BASE_CONDITION      = '[Player.Playing + !Window.IsVisible(fullscreeninfo) + Window.IsVisible(fullscreenvideo)]'


# ---------------------------------------------------------------- when (services.Player.__chkOnNext)

def roundup_div(p, q):
    """globals.roundupDIV, the same rules: ceil of p / q via divmod; 1 when q is 0."""
    try:
        d, r = divmod(p, q)
        if r: d += 1
        return d
    except ZeroDivisionError:
        return 1


def window(total_seconds, max_progress, timer):
    """(threshold, int_time): the stretch before a show's end in which Up next
    appears, with the formula the add-on has always used. `total_seconds` is the
    show's length, `max_progress` the Seek_Threshold setting (percent), `timer`
    ONNEXT_TIMER. It opens when the time left falls to `threshold` and closes when
    it falls below `int_time`."""
    total     = int(total_seconds * (max_progress / 100))
    threshold = abs((total - (total * .75)) - (timer * 3))
    return threshold, roundup_div(threshold, 3)


def in_window(played, remaining, threshold, int_time, min_duration):
    """True while Up next should be up: the show has played past Seek_Tolerance
    and the time left is inside the window."""
    return played > min_duration and (remaining <= threshold and remaining >= int_time)


# ---------------------------------------------------------------- what (Overlay, Background)

def time_format(region_time):
    """A strftime format for the next show's time from Kodi's
    xbmc.getRegion('time') ('%H:%M:%S', '%I:%M:%S %p', ...), without the
    seconds. Kodi turns 'hh' into '%I%I'; doubles are collapsed. '%H:%M' when
    the text is empty or not a usable time format."""
    if not isinstance(region_time, str): return DEFAULT_TIME_FORMAT
    fmt = region_time.strip()
    for double in ('%H%H', '%I%I', '%M%M', '%S%S'):
        while double in fmt: fmt = fmt.replace(double, double[:2])
    fmt = re.sub(r'\s*[:.]?%S', '', fmt).strip()
    if '%M' not in fmt or not ('%H' in fmt or '%I' in fmt): return DEFAULT_TIME_FORMAT
    if re.search(r'%(?![HIMp])', fmt): return DEFAULT_TIME_FORMAT
    return fmt


def _epoch(start):
    """The start as a positive epoch (int, float or numeric text), else None."""
    if isinstance(start, bool): return None
    if isinstance(start, str):
        try: start = float(start.strip())
        except ValueError: return None
    if not isinstance(start, (int, float)) or not math.isfinite(start) or start <= 0: return None
    return start


def next_time(start, fmt, kodi_label=''):
    """The next show's start as local time text: the guide's epoch `start` with
    `fmt`, else Kodi's VideoPlayer.NextStartTime label, else ''. Before this,
    a missing start returned nothing and the Kodi label was never reached."""
    epoch = _epoch(start)
    if epoch is not None:
        try: return datetime.datetime.fromtimestamp(epoch).strftime(fmt or DEFAULT_TIME_FORMAT)
        except (ValueError, OverflowError, OSError): pass
    return kodi_label.strip() if isinstance(kodi_label, str) else ''


def valid_string(s):
    """globals.validString, the same rules."""
    return "".join(x for x in s if (x.isalnum() or x not in '\\/:*?"<>|'))


def card_text(watching, next_label, now_title, chname, next_time_text, next_title):
    """The card's two lines, as the add-on has always written them:
    "You're Watching: <title> on <channel>[CR]Next @ <time>: [B]<next>[/B]"
    (the channel is left out when the title already contains it)."""
    on_now  = '%s on %s'%(now_title, chname) if chname not in valid_string(now_title) else now_title
    on_next = '@ %s: %s'%(next_time_text, next_title)
    return '%s %s[CR]%s [B]%s[/B]'%(watching, on_now, next_label, on_next)


# ---------------------------------------------------------------- where and how (Overlay)

def position(setting, width, height):
    """(x, y) of the card: the OnNext_Position_XY setting '(x,y)', else the
    automatic place (bottom left), the same rule as overlaytool's 'Auto'."""
    try:
        x, y = ast.literal_eval(setting)
        return int(x), int(y)
    except Exception:
        return abs(int(width // 9)), abs(int(height // 16) - height) - 356


def grid(width, height):
    """(fx, fy): how the skin's video window grid (Window.getWidth()/getHeight())
    compares with the 1920x1080 grid positions are given in; (1.0, 1.0) when
    the size is unusable. A 1080 skin gives (1.0, 1.0)."""
    for v in (width, height):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0: return 1.0, 1.0
    return width / BASE_W, height / BASE_H


def layout(x, y, fx=1.0, fy=1.0):
    """Each control's rectangle (x, y, w, h), as onnext.xml drew them: the 5 px
    frame, the artwork inside it, the text 140 px below the card top and its
    shadow 1 px right and down (where Kodi draws a font shadow). `x`, `y` are in
    the 1920x1080 grid; `fx`, `fy` (grid()) move them to the skin's grid."""
    x, y = int(round(x * fx)), int(round(y * fy))
    bx, by = int(round(CARD_BORDER * fx)), int(round(CARD_BORDER * fy))
    cw, ch = int(round(CARD_W * fx)), int(round(CARD_H * fy))
    tw, th, gap = int(round(TEXT_W * fx)), int(round(TEXT_H * fy)), int(round(TEXT_GAP * fy))
    return {'frame' : (x, y, cw, ch),
            'art'   : (x + bx, y + by, cw - 2*bx, ch - 2*by),
            'shadow': (x + SHADOW_OFFSET, y + gap + SHADOW_OFFSET, tw, th),
            'text'  : (x, y + gap, tw, th)}


def condition(prop):
    """The controls' visible condition: today's (playing, full-screen video, no
    info screen) plus the Home window property `prop` that the add-on sets while
    the card is up. Kodi plays 'Visible' and 'Hidden' animations only when a
    control's visible condition changes (GUIControl.cpp), not on setVisible."""
    return '%s + String.IsEqual(Window(Home).Property(%s),true)]'%(BASE_CONDITION[:-1], prop)


def animations(x, y):
    """Today's zoom-in (centred on the card) and fade-in, plus a 240 ms fade-out."""
    return [('Visible', 'effect=zoom start=80 end=100 center=%s,%s delay=160 tween=back time=240 reversible=false'%(x, y)),
            ('Visible', 'effect=fade end=100 time=240 reversible=false'),
            ('Hidden' , 'effect=fade start=100 end=0 time=240 reversible=false')]


# ---------------------------------------------------------------- when to hide (the wait thread)

def hide_reason(elapsed, show_seconds, idle_now, idle_last, epoch_now, epoch_shown, closed, playing, stopping):
    """'' while the card stays up, else why it goes (the first that applies):
    Kodi is shutting down, the overlay closed or another show replaced it,
    playback changed (zap, next programme, restart, stop), PseudoTV no longer
    plays, a button was pressed (Kodi's idle seconds went down), or its time is
    up (`show_seconds`)."""
    if stopping:                 return 'shutdown'
    if closed:                   return 'closed'
    if epoch_now != epoch_shown: return 'playback changed'
    if not playing:              return 'not playing'
    if idle_now < idle_last:     return 'button'
    if elapsed >= show_seconds:  return 'time up'
    return ''
