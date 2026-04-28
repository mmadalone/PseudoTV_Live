# Test-only stub for script.module.kodi-six. The real kodi_six package
# re-exports the xbmc.* modules with a Python 2 string-decoding shim;
# in our py3-only test environment, identity passthrough to the mocks is fine.
import xbmc          # noqa: F401
import xbmcaddon     # noqa: F401
import xbmcgui       # noqa: F401
import xbmcplugin    # noqa: F401
import xbmcvfs       # noqa: F401
