#   Copyright (C) 2025 Lunatixz
#
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

# madteevee (imports fork, imports.62): 'Restart from beginning' in the TV guide's
# context menu (addon.xml kodi.context.item; shown only on a playable show on now).
# It only hands the guide entry's own path to the running service as
# NotifyAll(pseudotv, restart, {path}); the service decides and restarts
# (services.Player.requestRestart, restart_helpers). JSON-RPC instead of the
# NotifyAll builtin, because builtin parameters are split on commas.
import json
import sys

from kodi_six import xbmc

import restart_helpers
from logger import log


def _run():
    try:    path = sys.listitem.getPath()
    except AttributeError: path = None
    request = restart_helpers.guide_request(path)
    if request is None:
        log('context_restart, not a guide entry: %s'%(path))
        return
    log('context_restart, %s'%(path), xbmc.LOGINFO)
    xbmc.executeJSONRPC(json.dumps({'jsonrpc':'2.0','id':1,'method':'JSONRPC.NotifyAll',
                                    'params':{'sender':restart_helpers.NOTIFY_SENDER,
                                              'message':restart_helpers.NOTIFY_MESSAGE,
                                              'data':request}}))


if __name__ == '__main__': _run()
