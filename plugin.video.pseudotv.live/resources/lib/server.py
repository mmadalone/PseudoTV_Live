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
#
# -*- coding: utf-8 -*-
import gzip, mimetypes, socket, time, threading, os, errno

from zeroconf                  import *
from globals                   import *
from functools                 import partial
from six.moves.BaseHTTPServer  import BaseHTTPRequestHandler, HTTPServer
from six.moves.socketserver    import ThreadingMixIn
from io                        import BytesIO

#todo proper REST API to handle server/client communication incl. sync/update triggers.
#todo incorporate experimental webserver UI to master branch.

ZEROCONF_SERVICE      = "_xbmc-jsonrpc-h._tcp.local."
COMPRESSION_THRESHOLD = 1024  # 1 KB
CHUNK_SIZE            = 4096 #4 KB

class Discovery:
    class MyListener(object):
        def __init__(self, multiroom=None):
            self.zServers  = dict()
            self.zeroconf  = Zeroconf()
            self.multiroom = multiroom

        def log(self, msg, level=xbmc.LOGDEBUG):
            return log('%s: %s'%(self.__class__.__name__,msg),level)

        def removeService(self, zeroconf, type, name):
            self.log("removeService, type = %s, name = %s"%(type,name))
             
        def addService(self, zeroconf, type, name):
            info = self.zeroconf.getServiceInfo(type, name)
            if info:
                IP = socket.inet_ntoa(info.getAddress())
                if IP != SETTINGS.getIP():
                    server = info.getServer()
                    self.zServers[server] = {'type':type,'name':name,'server':server,'host':'%s:%d'%(IP,info.getPort()),'bonjour':'http://%s:%s/%s'%(IP,SETTINGS.getSettingInt('TCP_PORT'),BONJOURFLE)}
                    self.log("addService, found zeroconf %s @ %s using bonjour %s"%(server,self.zServers[server]['host'],self.zServers[server]['bonjour']))
                    self.multiroom.addServer(requestURL(self.zServers[server]['bonjour'],json_data=True))
            
            
    def __init__(self, service=None, multiroom=None):
        self.service   = service
        self.multiroom = multiroom
        self._run()
                   

    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def _run(self):
        if not PROPERTIES.isRunning('Discovery'):
            with PROPERTIES.chkRunning('Discovery'):
                zconf = Zeroconf()
                zcons = self.multiroom._getStatus()
                self.log("_run, Multicast DNS Service waiting for (%s)"%(ZEROCONF_SERVICE))
                SETTINGS.setSetting('ZeroConf_Status','[COLOR=yellow][B]%s[/B][/COLOR]'%(LANGUAGE(32252)))
                ServiceBrowser(zconf, ZEROCONF_SERVICE, self.MyListener(multiroom=self.multiroom))
                self.service.monitor.waitForAbort(DISCOVER_INTERVAL)
                SETTINGS.setSetting('ZeroConf_Status',LANGUAGE(32211)%({True:'green',False:'red'}[zcons],{True:LANGUAGE(32158),False:LANGUAGE(32253)}[zcons]))
                self.log("_run, Multicast DNS Service stopping search for (%s)"%(ZEROCONF_SERVICE))
                zconf.close()
                        
            
class MyHandler(BaseHTTPRequestHandler):
    
    def __init__(self, request, client_address, server, service):
        self.service = service
        self.monitor = service.monitor
        try: BaseHTTPRequestHandler.__init__(self, request, client_address, server)
        except: pass


    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def do_HEAD(self):
        self.log('do_HEAD, incoming path = %s'%(self.path))
        self.send_response(200, "OK")
        self.send_header("Content-type", "*/*")
        self.end_headers()
        
        
    def do_POST(self):
        self.log('do_POST, incoming path = %s'%(self.path))
        def __verifyUUID(uuid):#lazy security check
            if uuid == SETTINGS.getMYUUID(): return True
            else:
                from multiroom  import Multiroom
                for server in list(Multiroom().getDiscovery().values()):
                    if server.get('uuid') == uuid: return True

        with PROPERTIES.suspendActivity():
            if self.path.lower().endswith('.json'):
                try:    incoming = loadJSON(self.rfile.read(int(self.headers['content-length'])).decode())
                except: incoming = {}
                if __verifyUUID(incoming.get('uuid')):
                    self.log('do_POST incoming uuid [%s] verified!'%(incoming.get('uuid')))
                    #channels - channel manager save
                    if self.path.lower() == '/%s'%(CHANNELFLE.lower()) and incoming.get('payload'):
                        from channels import Channels
                        if Channels().setChannels(list(Channels()._verify(incoming.get('payload')))):
                            DIALOG.notificationDialog(LANGUAGE(30085)%(LANGUAGE(30108),incoming.get('name',ADDON_NAME)))
                        return self.send_response(200, "OK")
                    #filelist w/resume - paused channel rule
                    elif self.path.lower().startswith('/filelist') and incoming.get('payload'):
                        print('do_POST',os.path.join(TEMP_LOC,self.path.replace('/filelist/','')),incoming.get('payload'))
                        if setJSON(os.path.join(TEMP_LOC,self.path.replace('/filelist/','')),incoming.get('payload')):
                            DIALOG.notificationDialog(LANGUAGE(30085)%(LANGUAGE(30060),incoming.get('name',ADDON_NAME)))
                        return self.send_response(200, "OK")
                    else: self.send_error(401, "Path Not found")
                else: return self.send_error(401, "UUID Not verified!")
            else: return self.do_GET()
                    
                
    def do_GET(self):
        self.log('do_GET, incoming path = %s' % (self.path))
        def __sendChunk(chunk, compress=False):
            self.log('do_GET, __sendChunk, chunk = %s, compress = %s'%(len(chunk),compress))
            if compress:
                chunk = gzip.compress(chunk, compresslevel=5)
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", len(chunk))
            self.end_headers() # Finalize headers before sending body
            self.wfile.write(chunk)

        def __sendFile(path, compress=False):
            self.log('do_GET, __sendFile path = %s, compress = %s' % (path, compress))
            with xbmcvfs.File(path) as fle:
                chunk = fle.readBytes()
            __sendChunk(chunk,compress)
            
        with PROPERTIES.suspendActivity():
            try:
                file_path       = self.path.lower()
                accept_encoding = self.headers.get("Accept-Encoding", "")
                use_compression = "gzip" in accept_encoding
                
                # Determine Content-Type
                if   file_path.endswith('.json'): content_type = "application/json"
                elif file_path.endswith('.m3u'):  content_type = "application/vnd.apple.mpegurl"
                elif file_path.endswith('.xml'):  content_type = "text/plain"
                elif file_path.endswith('.html'): content_type = "text/html"
                else:
                    guessed_type = mimetypes.guess_type(file_path[1:])[0]
                    content_type = guessed_type if guessed_type else "application/octet-stream"

                self.send_response(200)
                self.send_header("Content-type", content_type)
                
                if   file_path == '/favicon.ico': return self.send_response(204)  # 204 No Content
                elif file_path == f'/{M3UFLE.lower()}':             __sendFile(M3UFLEPATH, use_compression)
                elif file_path == f'/{GENREFLE.lower()}':           __sendFile(GENREFLEPATH, use_compression)
                elif file_path == f'/{XMLTVFLE.lower()}':           __sendFile(XMLTVFLEPATH, use_compression)
                elif file_path.startswith(('/images/', '/logos/')): __sendFile(unquoteString(self.path).replace('/images/', '').replace('/logos/', ''), use_compression)
                else:
                    chunk = b''
                    if   file_path == f'/{BONJOURFLE.lower()}': chunk = dumpJSON(SETTINGS.getBonjour(inclChannels=True),idnt=4).encode(encoding=DEFAULT_ENCODING)
                    elif file_path.startswith('/filelist'):     chunk = dumpJSON(getJSON((os.path.join(TEMP_LOC, self.path.replace('/filelist/', ''))))).encode(encoding=DEFAULT_ENCODING)
                    elif file_path.startswith('/remote'):
                        if   file_path.endswith('.json'):       chunk = dumpJSON(SETTINGS.getPayload(),idnt=4).encode(encoding=DEFAULT_ENCODING)
                        elif file_path.endswith('.html'): 
                            use_compression = False
                            chunk = SETTINGS.getPayloadUI().encode(encoding=DEFAULT_ENCODING)
                        else: return self.send_error(404, "File Not Found [%s]" % self.path)
                    else: return self.send_error(404, "File Not Found [%s]" % self.path)
                    self.log('do_GET, file_path = %s, use_compression = %s'%(file_path, use_compression))
                    __sendChunk(chunk, use_compression)
            except FileNotFoundError:
                self.send_error(404, "File Not Found [%s]" % self.path)
            except Exception as e:
                self.log_error("Exception during GET request: %s", e)
                self.send_error(500, "Internal Server Error")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    
    
class HTTP:
    httpd          = None
    isRunning      = False
    pendingRestart = False
    
    def __init__(self, service=None):
        self.log('__init__')
        self.service = service
        self.monitor = service.monitor
        
                    
    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def _chkPort(self, host, port=SETTINGS.getSettingInt('TCP_PORT')):
        def __isAvailable(host, tmpPort):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind((host, tmpPort))
                    return False
                except socket.error as e:
                    if e.errno == errno.EADDRINUSE:
                        return True
                    raise e

        tmpPort = port
        while not self.monitor.abortRequested() and __isAvailable(host, tmpPort):
            if self.monitor.waitForAbort(0.1): break
            else:
                self.log(f"_chkPort {tmpPort} is in use. Trying next port.")
                tmpPort += 1
        if tmpPort != port: DIALOG.notificationDialog(LANGUAGE(30097)%(port,tmpPort))
        self.log("_chkPort, port available = %s"%(tmpPort))
        return tmpPort
        
        
    def _restart(self):
        self.pendingRestart = True
       

    def _start(self, silent=False):  
        def __update(silent=True):
            if not silent: DIALOG.notificationDialog('%s: %s'%(SETTINGS.getSetting('Remote_NAME'),LANGUAGE(32211)%({True:'green',False:'red'}[self.isRunning],{True:LANGUAGE(32158),False:LANGUAGE(32253)}[self.isRunning])))
            SETTINGS.setSetting('Remote_Status',LANGUAGE(32211)%({True:'green',False:'red'}[self.isRunning],{True:LANGUAGE(32158),False:LANGUAGE(32253)}[self.isRunning]))

        def __cancel(wait=FIFTEEN):
            try:
                if self.httpd.is_alive():
                    if hasattr(self.httpd, 'cancel'): self.httpd.cancel()
                    try: self.httpd.join(wait)
                    except: pass
                return self.httpd.is_alive()
            except: pass
            
        def __stop(restart=False):
            try: self._server.shutdown()
            except: pass
            self.isRunning = False
            self.log('__stop, shutting server down, restart = %s, isAlive = %s'%(restart,__cancel()), xbmc.LOGINFO)
            if restart:
                self.pendingRestart = False
                self.service._que(self.service.tasks.chkHTTP,1)
            else: __update(restart)
            
        """Starts the threaded HTTP server with GZIP support."""
        while not self.monitor.abortRequested():
            if not self.isRunning:
                self.isRunning = True
                try: 
                    host   = SETTINGS.getIP()
                    port   = self._chkPort(host, SETTINGS.getSettingInt('TCP_PORT'))
                    server = PROPERTIES.setRemoteHost('%s:%s'%(host,port))
                    
                    self.log("_start, starting server @ %s"%(server),xbmc.LOGINFO)
                    SETTINGS.setSetting('Remote_NAME' ,SETTINGS.getFriendlyName())
                    SETTINGS.setSetting('Remote_M3U'  ,'http://%s/%s'%(server,M3UFLE))
                    SETTINGS.setSetting('Remote_XMLTV','http://%s/%s'%(server,XMLTVFLE))
                    SETTINGS.setSetting('Remote_GENRE','http://%s/%s'%(server,GENREFLE))
                    
                    self._server = ThreadedHTTPServer((host, port), partial(MyHandler,service=self.service))
                    self._server.allow_reuse_address = True
                    
                    self.httpd = Thread(target=self._server.serve_forever)
                    self.httpd.daemon=True
                    self.httpd.start()
                    __update(silent)
                except Exception as e: self.log("_start, startup Failed! %s"%(e), xbmc.LOGERROR)
            elif self.service._shutdown(FIFTEEN): break
        return __stop(self.pendingRestart)