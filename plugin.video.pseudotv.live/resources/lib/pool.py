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
 
from globals            import *
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, TimeoutError
from itertools          import repeat, count
from functools          import partial, wraps, reduce, update_wrapper

try:
    from _multiprocessing import SemLock, sem_unlink #workaround to raise two python exceptions. _multiprocessing import error, sem_unlink missing from native python (android).
    from multiprocessing  import cpu_count
    cpu_count = multiprocessing.cpu_count()
    xbmcgui.Window(10000).setProperty('%s.CPU_CYCLE'%(ADDON_ID),str(cpu_count))
    POOL_ENABLED = True #todo monkeypatch/wrapper to fix pickling error. 
except:
    POOL_ENABLED = False
    cpu_count = os.cpu_count()
    xbmcgui.Window(10000).setProperty('%s.CPU_CYCLE'%(ADDON_ID),str(cpu_count))
    
CPU_CYCLE = (1/int((xbmcgui.Window(10000).getProperty('%s.CPU_CYCLE'%(ADDON_ID)) or '1')) / 8) #lazy function

def wrapped_partial(func, *args, **kwargs):
    partial_func = partial(func, *args, **kwargs)
    update_wrapper(partial_func, func)
    return partial_func
    
def timeit(method):
    @wraps(method)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result     = method(*args, **kwargs)
        end_time   = time.time()
        if REAL_SETTINGS.getSetting('Debug_Enable').lower() == 'true':
            log('%s timeit => %.2f ms'%(method.__qualname__.replace('.',': '),(end_time-start_time)*1000))
        return result
    return wrapper

def killit(method):
    @wraps(method)
    def wrapper(wait=30, *args, **kwargs):
        class waiter(Thread):
            def __init__(self):
                Thread.__init__(self)
                self.result = None
                self.error  = None
            def run(self):
                try:    self.result = method(*args, **kwargs)
                except: self.error  = sys.exc_info()[0]
        timer = waiter()
        timer.name = '%s.%s'%('killit',method.__qualname__.replace('.',': '))
        timer.daemon=True
        timer.start()
        log('%s, killit starting %s waiting (%s)'%(method.__qualname__.replace('.',': => -:'),timer.name,wait))
        try: timer.join(wait)
        except: pass
        if (timer.is_alive() or timer.error): log('%s, killit Timed out! Errors: %s'%(method.__qualname__.replace('.',': '),timer.error), xbmc.LOGERROR)
        return timer.result
    return wrapper
    
def poolit(method):
    @wraps(method)
    def wrapper(items=[], wait=90, *args, **kwargs):
        class pooler(Thread):
            def __init__(self):
                Thread.__init__(self)
                self.result = None
                self.error  = None
            def run(self):
                try:    self.result = ExecutorPool().executors(method, items, *args, **kwargs)
                except: self.error  = sys.exc_info()[0]
        thread = pooler()
        thread.name = '%s.%s'%('poolit',method.__qualname__.replace('.',': '))
        thread.daemon=True
        thread.start()
        log('%s, poolit starting %s waiting (%s)'%(method.__qualname__.replace('.',': => -:'),thread.name,wait))
        try: thread.join(wait)
        except: pass
        if (thread.is_alive() or thread.error): log('%s, poolit Timed out! Errors: %s'%(method.__qualname__.replace('.',': '),thread.error), xbmc.LOGERROR)
        return thread.result
    return wrapper

def threadit(method):
    @wraps(method)
    def wrapper(*args, **kwargs):
        thread_name = 'threadit.%s'%(method.__qualname__.replace('.',': '))
        for thread in thread_enumerate():
            if thread.name == thread_name and thread.is_alive():
                if hasattr(thread, 'cancel'): 
                    thread.cancel()
                    log('%s, threadit canceling existing thread %s'%(method.__qualname__.replace('.',': '),thread_name))        
        thread = Thread(None, method, None, args, kwargs)
        thread.name = thread_name
        thread.daemon=True
        thread.start()
        log('%s, threadit starting %s'%(method.__qualname__.replace('.',': '),thread.name))
    return wrapper

def timerit(method):
    @wraps(method)
    def wrapper(wait=0.1, *args, **kwargs):
        thread_name = 'timerit.%s'%(method.__qualname__.replace('.',': '))
        for timer in thread_enumerate():
            if timer.name == thread_name and timer.is_alive():
                if hasattr(timer, 'cancel'): 
                    timer.cancel()
                    log('%s, timerit canceling existing timer %s'%(method.__qualname__.replace('.',': '),thread_name))    
                try: timer.join()    
                except: pass
        timer = Timer(float(wait), method, *args, **kwargs)
        timer.name = thread_name
        timer.start()
        log('%s, timerit starting %s wait = %s'%(method.__qualname__.replace('.',': '),thread_name,wait))
        try: timer.join(wait)
        except: pass
        return timer
    return wrapper  

def executeit(method):
    @wraps(method)
    def wrapper(*args, **kwargs):
        log('executeit => %s'%(method.__qualname__.replace('.',': ')))
        return ExecutorPool().executor(method, None, *args, **kwargs)
    return wrapper

class ExecutorPool:
    def __init__(self):
        self.CPUCount = self.getCPUCount() 
        if POOL_ENABLED: self.pool = ProcessPoolExecutor
        else:            self.pool = ThreadPoolExecutor
        self.log(f"__init__, multiprocessing = {POOL_ENABLED}, CORES = {cpu_count}, THREADS = {self.CPUCount}, CPU_CYCLE = {CPU_CYCLE}")


    def getCPUCount(self):
        if POOL_ENABLED: return cpu_count
        else:            return int(os.getenv('THREAD_COUNT', cpu_count))
            
            
    def log(self, msg, level=xbmc.LOGDEBUG):
        return log('%s: %s'%(self.__class__.__name__,msg),level)


    def executor(self, func, timeout=None, *args, **kwargs):
        self.log("executor, func = %s, timeout = %s"%(func.__name__,timeout))
        with self.pool(self.CPUCount) as executor:
            try: return executor.submit(func, *args, **kwargs).result(timeout)
            except Exception as e:
                self.log("executor, func = %s failed! %s\nargs = %s, kwargs = %s"%(func.__name__,e,args,kwargs), xbmc.LOGERROR)
                return self.execute(func, *args, **kwargs)


    def executors(self, func, items=[], *args, **kwargs):
        self.log("executors, func = %s, items = %s"%(func.__name__,len(items)))
        with self.pool(self.CPUCount) as executor:
            try: return list(filter(lambda item: item is not None, executor.map(wrapped_partial(func, *args, **kwargs), items)))
            except Exception as e:
                self.log("executors, func = %s, items = %s failed! %s\nargs = %s, kwargs = %s"%(func.__name__,len(items),e,args,kwargs), xbmc.LOGERROR)
                return self.generator(func, items, *args, **kwargs)


    def execute(self, func, *args, **kwargs):
        self.log("execute, func = %s"%(func.__name__))
        try: return func(*args, **kwargs)
        except Exception as e: self.log("execute, func = %s failed! %s\nargs = %s, kwargs = %s"%(func.__name__,e,args,kwargs), xbmc.LOGERROR)


    def generator(self, func, items=[], *args, **kwargs):
        self.log("generator, items = %s"%(len(items)))
        try: return list(filter(lambda item: item is not None, [wrapped_partial(func, *args, **kwargs)(i) for i in items]))
        except Exception as e: self.log("generator, func = %s, items = %s failed! %s\nargs = %s, kwargs = %s"%(func.__name__,len(items),e,args,kwargs), xbmc.LOGERROR)
