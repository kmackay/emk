from __future__ import print_function

import os
import sys
import imp
if sys.version_info[0] < 3:
    _string_type = basestring
    import __builtin__ as builtins
else:
    _string_type = str
    import builtins
import logging
import collections
import errno
import traceback
import time
import cPickle as pickle
import threading
import shutil
import multiprocessing
import re
import hashlib

_module_path = os.path.realpath(__file__)

class _Target(object):
    """
    Representation of a potential target (ie, a rule product).
    
    Properties should not be modified.
    
    Properties:
      orig_path -- The original (relative) path of the target, as specified to emk.rule() or @emk.make_rule.
      rule      -- The emk.Rule instance that generates this target.
      abs_path  -- The canonical path of the target.
      attached  -- The set of targets (canonical paths) that have been attached to this target. Only usable when a rule is executing.
    """
    def __init__(self, local_path, rule):
        self.orig_path = local_path
        self.rule = rule
        if rule:
            self.abs_path = _make_target_abspath(local_path, rule.scope)
        else:
            self.abs_path = local_path
            
        self.attached = set()
        
        self._rebuild_if_changed = False
        
        self._required_by = set()
        self._built = False
        self._visited = False
        
        self._virtual_modtime = None

class _Rule(object):
    """
    Representation of a rule.
    
    Properties should not be modified.
    
    Properties:
      func        -- The function to execute to run the rule. This should take at least 2 arguments: the list of canonical product paths,
                     the list of canonical requirement paths, and any additional args as desired.
      produces    -- The list of emk.Target instances that are produced by this rule.
      requires    -- The list of things that this rule depends on (canonical paths). These are the primary dependencies of the rule.
                     All primary dependencies of a rule are built before the rule is built. If a primary dependency does not
                     exist, a build error is raised.
      args        -- The arbitrary args that will be passed (unpacked) to the rule function (specified when the rule was created).
      cwd_safe    -- Whether or not the rule is cwd-safe (True or False). Specified when the rule was created. 
                     cwd-unsafe rules are executed sequentially in a single thread, and the current working directory
                     of the process is set the the scope dir for the rule before it is executed. cwd-safe rules
                     may be executed concurrently with any other rules, and the current working directory is not set.
      ex_safe     -- Whether or not the rule is exception safe (True or False). Specified when the rule was created. If an exception occurs while
                     a non-exception-safe rule is executing, emk will print a warning indicating that a rule was partially executed, and should be cleaned.
      has_changed -- The function to execute to determine if a requirement or product has changed. Uses emk.default_has_changed by default.
                     The function should take a single argument which is the absolute path of the thing to check to see if it has changed.
                     When this function is executing, emk.current_rule and emk.rule_cache() are available.
                          
      stack       -- The stack of where the rule was defined (a list of strings).
    
    Build-time properties (only usable when the rule is being executed):
      weak_deps      -- The set of weak dependencies (canonical paths) of this rule (added using emk.weak_depend()).
                        If a weak dependency does not exist, the rule is still built. If there is no cached information
                        for a weak dependency, the dependency is treated as "not changed" (so the rule will not be rebuilt).
                        Primarily used for dependencies that are discovered within the primary dependenceies (eg, header files).
      secondary_deps -- The set of extra dependencies (canonical paths) of this rule (added using emk.depend()).
                        All secondary dependencies of a rule are built before the rule is built. If a secondary dependency does not
                        exist, a build error is raised.
    """
    def __init__(self, requires, args, func, cwd_safe, ex_safe, has_changed, scope):
        self.func = func
        self.produces = []
        self.requires = requires
        self.args = args
        self.cwd_safe = cwd_safe
        self.ex_safe = ex_safe
        self.has_changed = has_changed
        
        self.scope = scope
        
        self._key = None
        self._cache = None
        self._untouched = set()
        
        self._lock = threading.Lock()
        
        self.secondary_deps = set()
        self.weak_deps = set()
        
        self._required_targets = []
        self._remaining_unbuilt_reqs = 0
        self._want_build = False
        self._built = False
        self._ran_func = False
        self.stack = []
        self._req_trace = {}

class _ScopeData(object):
    def __init__(self, parent, scope_type, scope_dir, proj_dir):
        self.parent = parent
        self.scope_type = scope_type
        self.dir = scope_dir
        self.proj_dir = proj_dir
        
        self._cache = None
        self._do_later_funcs = []
        
        if parent:
            self.default_modules = list(parent.default_modules)
            self.pre_modules = list(parent.pre_modules)
            self.build_dir = parent.build_dir
            self.module_paths = list(parent.module_paths)
            self.recurse_dirs = parent.recurse_dirs.copy()
        else:
            self.default_modules = []
            self.pre_modules = []
            self.build_dir = "__build__"
            self.module_paths = []
            self.recurse_dirs = set()
            
        self.modules = {} # map module name to instance (_Module_Instance)
        self.weak_modules = {}
        
        self.targets = {} # map original target name->target for the current scope
    
    def prepare_do_later(self):
        self._do_later_funcs = []

class _RuleQueue(object):
    """
    A threadsafe queue, servicing 1 "special" thread and 0 or more "normal" threads.
    The special thread is the only thread that is allowed to handle "special" queue items;
    it may also handle normal queue items. The normal threads only handle normal queue items.
    
    Basically this is for handling cwd-unsafe rules. cwd-safe rules are "normal" and may be handled by any thread; 
    cwd-unsafe rules are "special" and are only handled by the single special thread, so
    there is only ever one thread controlling the current working directory.
    
    It works as a normal threadsafe queue, but with a separate queue for the special items.
    The special thread checks to see if there are any items on the special queue; if not, it tries to
    grab an item from the normal queue; if there are no items on the normal queue either then it waits
    for an item to appear on the special queue.
    
    When a new item is added, it is added to the special queue if it is a special item. Otherwise, if
    the special queue is empty, and the special thread is not currently handling an item, the item is
    added to the special queue as well. Otherwise the item is added to the normal queue.
    
    This allows work to be shared equally among the threads, while still allowing the special items
    to be handled by only the special thread.
    """
    def __init__(self, num_threads):
        self.num_threads = num_threads
        self.lock = threading.Lock()

        self.join_cond = threading.Condition(self.lock)
        self.special_cond = threading.Condition(self.lock)
        self.cond = threading.Condition(self.lock)

        self.special_queue = collections.deque()
        self.queue = collections.deque()
        
        self.errors = []

        self.tasks = 0
        self.special_thread_busy = False

        self.STOP = object()

    def put(self, rule):
        with self.lock:
            if self.errors:
                return
            self.tasks += 1
            if not rule.cwd_safe or (not len(self.special_queue) and not self.special_thread_busy) or self.num_threads == 1:
                # not cwd_safe, or special queue is empty, so add to special queue
                self.special_queue.append(rule)
                self.special_cond.notify()
            else:
                # add to normal queue
                self.queue.append(rule)
                self.cond.notify()

    def get(self, special):
        with self.lock:
            if self.errors:
                return self.STOP # stop immediately
            if special:
                if len(self.special_queue):
                    item = self.special_queue.popleft()
                elif len(self.queue) and not (self.queue[0] is self.STOP):
                    # note that the special thread will only get STOP off of the special queue
                    item = self.queue.popleft()
                else:
                    while not len(self.special_queue):
                        self.special_cond.wait()
                    item = self.special_queue.popleft()
                self.special_thread_busy = True
                return item
            else:
                while not len(self.queue):
                    self.cond.wait()
                return self.queue.popleft()

    def done_task(self, special):
        with self.lock:
            if self.errors:
                return
            if special:
                self.special_thread_busy = False
            self.tasks -= 1
            if self.tasks == 0:
                self.join_cond.notifyAll()

    def join(self):
        with self.lock:
            while self.tasks and not self.errors:
                self.join_cond.wait()

    def stop(self):
        with self.lock:
            self.special_queue.append(self.STOP)
            self.special_cond.notify()

            num_threads = self.num_threads - 1
            while num_threads > 0:
                num_threads -= 1
                self.queue.append(self.STOP)
                self.cond.notify()
    
    def error(self, err):
        with self.lock:
            self.errors.append(err)
            self.join_cond.notifyAll()

class _Container(object):
    pass

_clean_log = logging.getLogger("emk.clean")
class _Clean_Module(object):
    """
    Module "clean".
    
    The clean module is automatically loaded in every rules scope. It provides the "clean" target,
    which will delete the build directory for the rules scope if the build directory is a subdirectory
    of the scope directory (so it won't delete the build directory if it is set to ".." for example).
    
    Properties:
      remove_build_dir -- If True, the clean rule will not delete the build directory. This is inherited
                          by child scopes.
    """
    def __init__(self, scope, parent=None):
        if parent:
            self.remove_build_dir = parent.remove_build_dir
        else:
            self.remove_build_dir = True
    
    def new_scope(self, scope):
        return _Clean_Module(scope, self)
    
    def remove_cache(self, build_dir):
        hash = hashlib.md5(emk.scope_dir).hexdigest()
        cache_path = os.path.join(build_dir, "__emk_cache__" + hash)
        _clean_log.debug("Removing cache %s", cache_path)
        try:
            os.remove(cache_path)
        except OSError:
            pass
    
    def clean_func(self, produces, requires):
        build_dir = os.path.realpath(os.path.join(emk.scope_dir, emk.build_dir))
        if self.remove_build_dir:
            if os.path.commonprefix([build_dir, emk.scope_dir]) == emk.scope_dir:
                _clean_log.info("Removing directory %s", build_dir)
                shutil.rmtree(build_dir, ignore_errors=True)
            else:
                self.remove_cache(build_dir)
        else:
            self.remove_cache(build_dir)
        
        emk.mark_virtual(*produces)
    
    def post_rules(self):
        emk.rule(self.clean_func, ["clean"], [emk.ALWAYS_BUILD], cwd_safe=True, ex_safe=True)

class _Module_Instance(object):
    def __init__(self, name, instance, mod):
        self.name = name # module name
        self.instance = instance # module instance
        self.mod = mod # python module that was imported

class _Always_Build(object):
    def __repr__(self):
        return "emk.ALWAYS_BUILD"

class _BuildError(Exception):
    def __init__(self, msg, extra_info=None):
        self.msg = msg
        self.extra_info = extra_info
    def __str__(self):
        return self.msg

def _style_tag(tag):
    return "\000\001" + tag + "\001\000"

class _NoStyler(object):
    def __init__(self):
        self.r = re.compile("\000\001([0-9A-Za-z_]*)\001\000")
        
    def style(self, string, record):
        return self.r.sub('', string)

class _PassthroughStyler(object):
    """
    Passthrough styler. Does not modify the emk style tags in any way.
    """
    def style(self, string, record):
        return string
        
class _ConsoleStyler(object):
    """
    Console styler. Converts the emk style tags into ANSI escape codes.
    
    Properties:
      styles -- A dict containing "tag": <escape code> mappings. You may modify this dict to change
                the output or add additional mappings.
    """
    def __init__(self):
        self.r = re.compile("\000\001([0-9A-Za-z_]*)\001\000")
        self.styles = {"bold":"\033[1m", "u":"\033[4m", "red":"\033[31m", "green":"\033[32m", "blue":"\033[34m",
            "important":"\033[1m\033[31m", "rule_stack":"\033[34m", "stderr":"\033[31m"}
        
    def style(self, string, record):
        r = self.r
        styles = self.styles.copy()
        if record.levelno >= logging.WARNING:
            styles["logtype"] = "\033[1m"
        if record.levelno >= logging.ERROR:
            styles["logtype"] = "\033[1m\033[31m"
        stack = []
        start = 0
        bits = []
        if record.levelno == logging.DEBUG:
            bits.append("\033[34m")
        m = r.search(string, start)
        while m:
            bits.append(string[start:m.start(0)])
            style = styles.get(m.group(1))
            if style is not None:
                stack.append(style)
                bits.append(style)
            elif m.group(1) == '' or m.group(1) == '__end__':
                prevstyle = stack.pop()
                if prevstyle:
                    bits.append("\033[0m")
                    bits.append(''.join(stack))
            else:
                stack.append('')
            
            start = m.end(0)
            m = r.search(string, start)
        bits.append(string[start:])
        bits.append("\033[0m")
        return ''.join(bits)
        
class _HtmlStyler(object):
    """
    HTML styler. Converts the emk style tags into CSS classes.
    
    Each log record (ie, an entire log message) is surrounded by <div class="emk_log emk_$levelname>...<\div>" tags,
    where the $levelname is debug, info, warning etc. Each emk style tag is converted into a <span class="emk_$tag">...<\span>,
    with $tag being the emk tag string.
    
    The characters '<', '>', and '&' are escaped for HTML. Four consecutive spaces will be replaced with four non-breaking spaces
    for indentation. Newlines in log messages will be repalced with <br>.
    
    Using this styler, the emk output can be inserted into an HTML page with appropriate CSS to make it look fancy.
    
    Example CSS:
      <style type="text/css">
      div.emk_log {margin: 0.2em 0; font-family: Arial, Helvetica, sans-serif;}
      div.emk_debug {color:blue;}
      .emk_warning .emk_logtype {font-weight:bold;}
      .emk_error .emk_logtype {font-weight:bold; color:red;}
      .emk_important {font-weight:bold; color:red;}
      .emk_stderr {color:red;}
      .emk_u {text-decoration:underline;}
      .emk_bold {font-weight:bold;}
      .emk_rule_stack {color:blue;}
      div.emk_log:nth-child(even) { background-color:#fff; }
      div.emk_log:nth-child(odd) { background-color:#eee; }
      </style>
    """
    def __init__(self):
        self.div_regex = re.compile("\000\001__start__\001\000")
        self.end_div_regex = re.compile("\000\001__end__\001\000" + r'\s*')
        self.span_regex = re.compile("\000\001([0-9A-Za-z_]+)\001\000")
        
    def style(self, string, record):
        string = string.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        string = self.div_regex.sub('<div class="emk_log emk_%s">' % (record.orig_levelname), string)
        string = self.end_div_regex.sub('</div>', string)
        string = self.span_regex.sub(r'<span class="emk_\1">', string)
        string = string.replace("\000\001\001\000", '</span>').replace("\n", "<br>").replace("    ", "&nbsp;&nbsp;&nbsp;&nbsp;")
        return string

class _Formatter(logging.Formatter):
    """
    Formatter for emk log messages.
    
    This formatter converts the log levelname to lowercase. If "extra={'adorn':False}" was passed to the log call,
    the message will be passed to the styler as-is. Otherwise, the format string is interpolated using the log record's __dict__.
    
    Properties:
      format_str -- The format string to use for normal ("adorned") log messages.
      styler     -- The log styler to use to convert emk style tags.
    """
    def __init__(self, format):
        self.format_str = format
        self.styler = _NoStyler()

    def format(self, record):
        record.message = record.getMessage()
        record.orig_levelname = record.levelname.lower()
        record.levelname = _style_tag('logtype') + record.levelname.lower() + _style_tag('')
            
        if record.__dict__.get("adorn") is False:
            return self.styler.style(_style_tag('__start__') + record.message + _style_tag('__end__'), record)
        
        return self.styler.style(_style_tag('__start__') + (self.format_str % (record.__dict__)) + _style_tag('__end__'), record)

class _WindowsOutputHandler(logging.StreamHandler):
    def __init__(self, stream=None):
        super(_WindowsOutputHandler, self).__init__(stream)

        self._windows_color_map = {
            0: 0x00, # black
            1: 0x04, # red
            2: 0x02, # green
            3: 0x06, # yellow
            4: 0x01, # blue
            5: 0x05, # magenta
            6: 0x03, # cyan
            7: 0x07, # white
            }
            
        self._handle = None
        import ctypes
        
        try:
            fd = stream.fileno()
            if fd in (1, 2): # stdout or stderr
                self._handle = ctypes.windll.kernel32.GetStdHandle(-10 - fd)
                self.set_text_attr = ctypes.windll.kernel32.SetConsoleTextAttribute
        except IOError:
            pass

        self.r = re.compile("\033\[([0-9]+)m")
        self._default_style = 0x07
        
    def _get_style(self, current_style, ansi_code):
        if ansi_code == 0:
            return self._default_style
        elif ansi_code == 1:
            return current_style | 0x08
        elif 30 <= ansi_code <= 37:
            return (current_style & ~0x07) | self._windows_color_map[ansi_code - 30]
        elif 40 <= ansi_code <= 47:
            return (current_style & ~0x70) | (self._windows_color_map[ansi_code - 40] << 4)
        else:
            return current_style

    def _output(self, string):
        _handle = self._handle
        stream = self.stream
        r = self.r
        start = 0
        current_style = self._default_style
        m = r.search(string, start)
        while m:
            stream.write(string[start:m.start(0)])
            
            code = int(m.group(1))
            current_style = self._get_style(current_style, code)
            if _handle:
                self.set_text_attr(_handle, current_style)

            start = m.end(0)
            m = r.search(string, start)
        stream.write(string[start:])

    def emit(self, record):
        try:
            msg = self.format(record)
            self._output(msg)
            self.stream.write('\n')
            self.flush()
        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            self.handleError(record)

def _find_project_dir(path):
    dir = path
    prev = None
    while dir != prev:
        if os.path.isfile(os.path.join(dir, "emk_project.py")):
            return dir
            
        prev = dir
        dir, tail = os.path.split(dir)
    return dir

def _try_call_method(mod, name):
    try:
        func = getattr(mod.instance, name)
    except AttributeError:
        return

    try:
        func()
    except _BuildError:
        raise
    except Exception:
        raise _BuildError("Error running %s.%s()" % (mod.name, name), _get_exception_info())

def _flatten_gen(args):
    # generator to convert args into a list of strings
    # args might be a string, or a list containing strings or lists
    global _string_type
    if isinstance(args, (_string_type, _Always_Build)):
        yield args
    else: # assume a list-like thing
        for arg in args:
            for s in _flatten_gen(arg):
                yield s

def _get_exception_info():
    t, value, trace = sys.exc_info()
    stack = traceback.extract_tb(trace)
    lines = ["%s: %s" % (t.__name__, str(value))]
    lines.extend(_format_stack(_filter_stack(stack)))
    return lines

emk_dev = False
def _filter_stack(stack):
    """
    Filter the given stack to remove leading internal stack frames that are not useful for users.
    """
    if emk_dev:
        return stack
        
    new_stack = []
    entered_emk = False
    left_emk = False
    for item in stack:
        if left_emk:
            new_stack.append(item)
        elif entered_emk:
            if not __file__.startswith(item[0]):
                left_emk = True
                new_stack.append(item)
        elif __file__.startswith(item[0]):
            entered_emk = True
    
    if not entered_emk:
        return stack
    return new_stack

def _format_stack(stack):
    return ["%s line %s, in %s: '%s'" % item for item in stack]

def _format_decorator_stack(stack):
    if not stack:
        return stack
    
    first_line = stack[0]
    result = ["%s line %s (or later), in %s" % (first_line[0], first_line[1]+1, first_line[2])]
    result.extend(_format_stack(stack[1:]))
    return result

def _make_abspath(rel_path, scope):
    """
    Make a path absolute, using the scope dir as a base for relative paths.
    """
    if os.path.isabs(rel_path):
        return rel_path
    return os.path.join(scope.dir, rel_path)

def _make_target_abspath(rel_path, scope):
    """
    Return a canonical target path based on the current scope. The proj and build dir placeholders will
    be replaced according to the current scope.
    """
    if rel_path.startswith(emk.proj_dir_placeholder):
        rel_path = rel_path.replace(emk.proj_dir_placeholder, scope.proj_dir, 1)
    if os.path.isabs(scope.build_dir):
        start, sep, end = rel_path.partition(emk.build_dir_placeholder)
        if sep:
            path = os.path.realpath(scope.build_dir + end)
        else:
            path = rel_path
    else:
        path = rel_path.replace(emk.build_dir_placeholder, scope.build_dir)
    return os.path.realpath(_make_abspath(path, scope))

def _make_require_abspath(rel_path, scope):
    """
    Return an absolute require path based on the current scope. The proj dir placeholder will
    be replaced according to the current scope; the build dir placeholder will not be replaced until
    later, in case the build dir for the given path is not yet known.
    """
    if rel_path is emk.ALWAYS_BUILD:
        return emk.ALWAYS_BUILD

    if rel_path.startswith(emk.proj_dir_placeholder):
        rel_path = rel_path.replace(emk.proj_dir_placeholder, scope.proj_dir, 1)
    return os.path.realpath(_make_abspath(rel_path, scope))

class EMK_Base(object):
    """
    Private implementation details of emk. The public API is derived from this class.
    """
    def __init__(self, args):
        global emk_dev
        
        self._flatten_gen = _flatten_gen
        
        self.log = logging.getLogger("emk")
        if sys.platform == "win32":
            handler = _WindowsOutputHandler(sys.stdout)
        else:
            handler = logging.StreamHandler(sys.stdout)
        self.formatter = _Formatter("%(name)s (%(levelname)s): %(message)s")
        handler.setFormatter(self.formatter)
        self.log.addHandler(handler)
        self.log.propagate = False
        
        self.build_dir_placeholder = "$:build:$"
        self.proj_dir_placeholder = "$:proj:$"
        
        global _module_path
        self._emk_dir, tail = os.path.split(_module_path)
        
        self._local = threading.local()
        self._local.current_rule = None
        
        self._prebuild_funcs = [] # list of (scope, func)
        self._postbuild_funcs = [] # list of (scope, func)
        
        self._targets = {} # map absolute target name -> target
        self._rules = []
        self._bad_rules = []
        
        self._visited_dirs = {}
        self._current_proj_dir = None
        self._stored_subproj_scopes = {} # map subproj path to loaded subproj scope (_ScopeData)
        self._known_build_dirs = {} # map path to build dir defined for that path
        
        self._all_loaded_modules = {} # map absolute module path to module
        
        self.ALWAYS_BUILD = _Always_Build()
        
        self._auto_targets = set()
        self._aliases = {}
        self._attached_dependencies = {}
        self._secondary_dependencies = {}
        self._weak_dependencies = {}
        self._requires_rule = set()
        self._rebuild_if_changed = set()
        
        self._fixed_aliases = {}
        self._fixed_auto_targets = []
        self._must_build = []
        
        self._done_build = False
        self._added_rule = False
        
        self._cleaning = False
        self._building = False
        self._did_run = False
        
        self._toplevel_examined_targets = set()
        
        self._lock = threading.Lock()
        
        # parse args
        log_levels = {"debug":logging.DEBUG, "info":logging.INFO, "warning":logging.WARNING, "error":logging.ERROR, "critical":logging.CRITICAL}
        
        self._options = {}
        
        self.log.setLevel(logging.INFO)
        self._options["log"] = "info"
        
        self._options["emk_dev"] = "no"
        
        self._build_threads = multiprocessing.cpu_count()
        self._options["threads"] = "x"
        
        stylers = {"no":_NoStyler, "console":_ConsoleStyler, "html":_HtmlStyler, "passthrough":_PassthroughStyler}
        
        self._options["style"] = "console"
        
        self.traces = set()
        self._trace_unchanged = False
        self._options["trace_unchanged"] = "no"
        
        self._explicit_targets = set()
        for arg in args:
            if '=' in arg:
                key, eq, val = arg.partition('=')
                if key == "explicit_target":
                    self._explicit_targets.add(val)
                else:
                    if key == "log":
                        level = val.lower()
                        if level in log_levels:
                            self.log.setLevel(log_levels[level])
                        else:
                            self.log.error("Unknown log level '%s'", level)
                    elif key == "emk_dev" and val == "yes":
                        emk_dev = True
                    elif key == "threads":
                        if val != "x":
                            try:
                                val = int(val, base=0)
                                if val < 1:
                                    val = 1
                            except ValueError:
                                self.log.error("Thread count '%s' cannot be converted to an integer", val)
                                val = 1
                            self._build_threads = val
                    elif key == "style":
                        if val not in stylers:
                            self.log.error("Unknown log style option '%s'", val)
                            val = "no"
                    elif key == "trace":
                        self.traces = set(val.split(','))
                    elif key == "trace_unchanged" and val == "yes":
                        self._trace_unchanged = True
                            
                    self._options[key] = val
            else:
                self._explicit_targets.add(arg)
        
        self.formatter.styler = stylers[self._options["style"]]()
        
        if "clean" in self._explicit_targets:
            self._cleaning = True
            self._explicit_targets = set(["clean"])
    
    scope = property(lambda self: self._local.current_scope)
    
    def _import_from(self, paths, name, set_scope_dir=False):
        if self.building:
            stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
            raise _BuildError("Cannot call import_from() when building", stack)

        fixed_paths = [_make_target_abspath(path, self.scope) for path in _flatten_gen(paths)]

        oldpath = os.getcwd()
        fp = None
        try:
            fp, pathname, description = imp.find_module(name, fixed_paths)
            mpath = os.path.realpath(pathname)
            d, tail = os.path.split(mpath)
            os.chdir(d)
            if set_scope_dir:
                self.scope.dir = d
            return imp.load_module(name, fp, pathname, description)
        except ImportError:
            self.log.info("Could not import '%s' from %s", name, fixed_paths)
        except _BuildError:
            raise
        except Exception as e:
            raise _BuildError("Error importing '%s' from %s" % (name, fixed_paths), _get_exception_info())
        finally:
            os.chdir(oldpath)
            if fp:
                fp.close()
        return None
    
    def _get_target(self, path, create_new=False):
        t = self._targets.get(path)
        if t is not None:
            return t
        t = self._fixed_aliases.get(path)
        if t is not None:
            return t
        if create_new:
            self.log.debug("Creating artificial target for %s", path)
            target = _Target(path, None)
            self._targets[path] = target
            return target

    def _resolve_build_dirs(self, dirs):
        # fix paths (convert $:build:$)
        updated_paths = []
        for path in dirs:
            if path is self.ALWAYS_BUILD:
                updated_paths.append(path)
                continue

            begin, build, end = path.partition(self.build_dir_placeholder)
            if build:
                d = os.path.dirname(begin)
                if d in self._known_build_dirs:
                    bd = self._known_build_dirs[d]
                    if os.path.isabs(bd):
                        n = os.path.realpath(bd + end)
                    else:
                        n = begin + bd + end
                    updated_paths.append(n)
                    self.log.debug("Fixed %s in path: %s => %s" % (self.build_dir_placeholder, path, n))
                else:
                    raise _BuildError("Could not resolve %s for path %s" % (self.build_dir_placeholder, path))
            else:
                updated_paths.append(path)
        return updated_paths

    def _fix_explicit_targets(self):
        fixed_targets = set()
        for target in self._explicit_targets:
            fixed_targets.add(_make_target_abspath(target, self.scope))
        self._explicit_targets = fixed_targets
    
    def _fix_traces(self):
        fixed_traces = set()
        for trace in self.traces:
            fixed_traces.add(_make_target_abspath(trace, self.scope))
        self.traces = fixed_traces
    
    def _remove_artificial_targets(self):
        real_targets = {}
        for path, target in self._targets.items():
            if target.rule:
                real_targets[path] = target
        self._targets = real_targets

    def _fix_aliases(self):
        unfixed_aliases = self._aliases.copy()
        fixed_aliases = {}
        did_fix = True
        while unfixed_aliases and did_fix:
            unfixed = {}
            did_fix = False
            for alias, target in unfixed_aliases.items():
                t = self._targets.get(target)
                if t is not None:
                    did_fix = True
                    fixed_aliases[alias] = t
                    self.log.debug("Fixed alias %s => %s", alias, t.abs_path)
                elif target in fixed_aliases:
                    did_fix = True
                    fixed_aliases[alias] = fixed_aliases[target]
                    self.log.debug("Fixed alias %s => %s", alias, fixed_aliases[target].abs_path)
                else:
                    unfixed[alias] = target
            unfixed_aliases = unfixed

        # can't fix any more, so assume the rest refer to files with no rules
        for alias, target in unfixed_aliases.items():
            self.log.debug("could not fix alias %s => %s", alias, target)
            t = _Target(target, None)
            self._targets[target] = t
            fixed_aliases[alias] = t

        self._fixed_aliases = fixed_aliases
    
    def _fix_depends(self):
        leftovers = {}
        for path, depends in self._secondary_dependencies.items():
            target = self._get_target(path)
            if target:
                if target._built:
                    raise _BuildError("Cannot add secondary dependencies to '%s' since it has already been built" % (target.abs_path))
                
                new_deps = set(self._resolve_build_dirs(depends))
                
                target.rule.secondary_deps |= new_deps
            else:
                self.log.debug("Target %s had secondary dependencies, but there is no rule for it yet", path)
                leftovers[path] = depends
        
        self._secondary_dependencies = leftovers
    
    def _fix_weak_depends(self):
        leftovers = {}
        for path, depends in self._weak_dependencies.items():
            target = self._get_target(path)
            if target:
                if target._built:
                    self.log.info("Cannot add weak dependencies to '%s' since it has already been built" % (target.abs_path))
                    continue
                
                new_deps = set(self._resolve_build_dirs(depends))
                
                target.rule.weak_deps |= new_deps
            else:
                self.log.debug("Target %s had weak dependencies, but there is no rule for it yet", path)
                leftovers[path] = depends
        
        self._weak_dependencies = leftovers

    def _fix_attached(self):
        for path, attached in self._attached_dependencies.items():
            target = self._get_target(path)
            if target:
                new_deps = set(self._resolve_build_dirs(attached))
                target.attached = target.attached.union(new_deps)
                if target._built: # need to build now, since it was attached to something that was already built
                    l = [self._get_target(a) for a in new_deps]
                    dep_targets = [t for t in l if t]
                    for d in dep_targets:
                        if not d._built:
                            self._must_build.append(d)
            else:
                self.log.debug("Target %s was attached to, but not yet defined as a product of a rule", path)

    def _fix_requires(self, rule):
        if rule._built:
            return

        rule.requires = self._resolve_build_dirs(rule.requires)
        
        secondaries = set(self._resolve_build_dirs(rule.secondary_deps))
        updated_paths = secondaries.union(set(rule.requires))
        
        weak_secondaries = set(self._resolve_build_dirs(rule.weak_deps)) - updated_paths

        # convert paths to actual targets
        required_targets = []   
        for path in updated_paths:
            target = self._get_target(path, create_new=True)

            target._required_by.add(rule) # when the target is built, we need to know which rules to examine for further building
            required_targets.append((target, False))
            
        for path in weak_secondaries:
            target = self._get_target(path, create_new=True)
            if target.rule:
                target._required_by.add(rule)
            required_targets.append((target, True))

        rule._required_targets = required_targets

    def _fix_auto_targets(self):
        self._fixed_auto_targets = []
        for path in self._auto_targets:
            self._fixed_auto_targets.append(self._get_target(path, create_new=True))
        self._auto_targets.clear() # don't need these anymore since they will be built from the fixed list if necessary
    
    def _fix_requires_rule(self):
        self._requires_rule = set(self._resolve_build_dirs(self._requires_rule))
    
    def _fix_rebuild_if_changed(self):
        for path in self._resolve_build_dirs(self._rebuild_if_changed):
            t = self._targets.get(path)
            if t:
                t._rebuild_if_changed = True
        
    def _setup_rule_cache(self, rule):
        paths = [t.abs_path for t in rule.produces]
        paths.sort()
        rule._key = key = hashlib.md5('\0'.join(paths)).hexdigest()
        cache = rule.scope._cache.setdefault("rules", {}).setdefault(key, {})
        rule._cache = cache
    
    def _toplevel_examine_target(self, target):
        if not target in self._toplevel_examined_targets:
            self._toplevel_examined_targets.add(target)
            self._examine_target(target, False)

    def _examine_target(self, target, weak):
        if target._visited or target._built:
            return
        target._visited = True
        self.log.debug("Examining target %s", target.abs_path)

        for path in target.attached:
            t = self._get_target(path)
            if t:
                self._examine_target(t, False)

        if not target.rule:
            if target.abs_path is self.ALWAYS_BUILD:
                target._built = True
            elif target.abs_path in self._requires_rule:
                self._need_undefined_rule = True
            else:
                if os.path.exists(target.abs_path):
                    target._built = True
                elif weak:
                    self.log.debug("Allowing weak dependency %s to not exist", target.abs_path)
                else:
                    self._need_undefined_rule = True
        else:
            rule = target.rule
            if rule._key is None:
                self._setup_rule_cache(rule)
            if not rule._want_build:
                rule._want_build = True
                rule._remaining_unbuilt_reqs = 0
                for req, is_weak in rule._required_targets:
                    self._examine_target(req, is_weak)
                    if (req.rule or not is_weak) and (not req._built): # we can't build this rule immediately
                        rule._remaining_unbuilt_reqs += 1

                if not rule._remaining_unbuilt_reqs:
                    # can build this rule immediately
                    self._buildable_rules.put(rule)

    def _done_rule(self, rule, built):
        now = time.time()
        for t in rule.produces:
            abs_path = t.abs_path
            cache = rule._cache.setdefault(abs_path, {})
            virtual = cache.get("virtual", False)
            if built:
                changed = (t.abs_path not in rule._untouched)
                
                if virtual:
                    if changed:
                        t._virtual_modtime = cache["vmodtime"] = now
                else:
                    t._virtual = False
                    try:
                        cache["modtime"] = os.path.getmtime(abs_path)
                    except OSError:
                        rulestack = ["    " + _style_tag('rule_stack') + line + _style_tag('') for line in rule.stack]
                        with self._lock:
                            del rule.scope._cache["rules"][rule._key]
                        raise _BuildError("%s should have been produced by the rule" % (abs_path), rulestack)
            else:
                changed = False
            
            if virtual and not changed:
                modtime = cache.setdefault("vmodtime", 0)
                t._virtual_modtime = modtime
        
            t._built = True

        for t in rule.produces:
            for r in t._required_by:
                if not r._want_build:
                    continue
                with r._lock:
                    r._remaining_unbuilt_reqs -= 1
                    if r._remaining_unbuilt_reqs == 0:
                        self._buildable_rules.put(r)
    
    def _add_bad_rule(self, rule):
        if not rule.ex_safe:
            with self._lock:
                self._bad_rules.append(rule)
    
    def _req_has_changed(self, rule, req):
        if req.abs_path is self.ALWAYS_BUILD:
            return True
            
        virtual_modtime = req._virtual_modtime
        if virtual_modtime is None:
            return rule.has_changed(req.abs_path)
        else:
            cache = rule._cache.setdefault(req.abs_path, {})
            cached_modtime = cache.get("vmodtime")
            if cached_modtime != virtual_modtime:
                self.log.debug("Modtime (virtual) for %s has changed; cached = %s, actual = %s", req.abs_path, cached_modtime, virtual_modtime)
                cache["vmodtime"] = virtual_modtime
                return True
            return False
    
    def default_has_changed(self, abs_path):
        """
        Default function for determining if a rule dependency has changed. Returns True if the file modtime
        of the dependency differs from the cached value, or if there is no cached value. Returns None if
        the file does not exist.
        
        Note that when a has_changed function is executing, the rule that needs the dependency is available
        via emk.current_rule; the rule cache is accessible via emk.rule_cache().
        
        Arguments:
          abs_path -- The absolute path of the dependency to check.
        """
        try:
            cache = self.rule_cache(abs_path)
            modtime = os.path.getmtime(abs_path)
            cached_modtime = cache.get("modtime")
            if cached_modtime != modtime:
                self.log.debug("Modtime for %s has changed; cached = %s, actual = %s", abs_path, cached_modtime, modtime)
                cache["modtime"] = modtime
                return True
            return False
        except OSError:
            return None
    
    def _get_changed_reqs(self, rule):
        changed_reqs = []
        for req, weak in rule._required_targets:
            if req.abs_path is self.ALWAYS_BUILD:
                c = True
                changed_reqs.append(req.abs_path)
            else:
                c = self._req_has_changed(rule, req)
                if c is None:
                    # it is OK for weak dependencies to not exist
                    if not weak:
                        raise _BuildError("Failed to determine if %s has changed", req.abs_path)
                elif c:
                    changed_reqs.append(req.abs_path)
            rule._req_trace[req] = c
        return changed_reqs
    
    def _build_thread_func(self, special):
        self._local.current_rule = None
        while(True):
            rule = self._buildable_rules.get(special)
            if rule is self._buildable_rules.STOP:
                return
            
            try:
                self._local.current_scope = rule.scope
                self._local.current_rule = rule

                need_build = False
                changed_reqs = self._get_changed_reqs(rule)
                rule._built = True
                
                if changed_reqs:
                    need_build = True
                    self.log.debug("Need to build %s because dependencies %s have changed", [t.abs_path for t in rule.produces], changed_reqs)
                else:
                    for t in rule.produces:
                        tcache = rule._cache.setdefault(t.abs_path, {})
                        if not tcache.get("virtual", False): # virtual products of this rule cannot be modified externally
                            if not os.path.exists(t.abs_path):
                                self.log.debug("Need to build %s because it does not exist", t.abs_path)
                                need_build = True
                            elif t._rebuild_if_changed and rule.has_changed(t.abs_path):
                                self.log.debug("Need to build %s because it has changed", t.abs_path)
                                need_build = True

                if need_build:
                    produces = [p.abs_path for p in rule.produces]
                    
                    if not rule.cwd_safe:
                        os.chdir(rule.scope.dir)
                    rule.func(produces, rule.requires, *rule.args)
                    rule._ran_func = True

                self._local.current_rule = None
                self._done_rule(rule, need_build)
            except _BuildError as e:
                self._add_bad_rule(rule)
                self._buildable_rules.error(e)
                return
            except Exception as e:
                self._add_bad_rule(rule)
                lines = ["    %s" % (line) for line in _get_exception_info()]
                lines.append("Rule definition:")
                lines.extend(["    " + _style_tag('rule_stack') + line + _style_tag('') for line in rule.stack])
                self._buildable_rules.error(_BuildError("Error running rule", lines))
                return
            except:
                self._add_bad_rule(rule)
                raise
            
            self._buildable_rules.done_task(special)

    def _do_build(self):
        # if there are explicit targets, see if we have rules for all of them
        # if so, just build those targets (and we should stop the build process)
        # otherwise, build all explicit targets and autobuild targets that we can

        self._building = True

        self._buildable_rules = _RuleQueue(self._build_threads)
        
        # mark unbuilt targets as unvisited
        for path, target in self._targets.items():
            if not target._built:
                target._visited = False
                if target.rule:
                    target.rule._want_build = False
        
        self._need_undefined_rule = False
        
        # revisit all targets that we want to build that were not built previously
        for target in self._toplevel_examined_targets:
            if not target._built:
                self._examine_target(target, False)

        for target in self._must_build:
            self._toplevel_examine_target(target)

        if not self._done_build:
            leftover_explicit_targets = set()
            if self._explicit_targets:
                for target in self._explicit_targets:
                    t = self._get_target(target)
                    if t:
                        self._toplevel_examine_target(t)
                    else:
                        leftover_explicit_targets.add(target)

            if (not self._explicit_targets) or leftover_explicit_targets or self._need_undefined_rule:
                for target in self._fixed_auto_targets:
                    self._toplevel_examine_target(target)

            if (self._explicit_targets and not leftover_explicit_targets):
                self._done_build = True

            self._explicit_targets = leftover_explicit_targets
        
        threads = []
        threads.append(threading.Thread(target=self._build_thread_func, kwargs={"special":True}))
        threads_left = self._build_threads - 1
        while threads_left > 0:
            threads_left -= 1
            threads.append(threading.Thread(target=self._build_thread_func, kwargs={"special":False}))
        
        for thread in threads:
            thread.start()
        
        try:
            self._buildable_rules.join()
        except KeyboardInterrupt:
            self._buildable_rules.error(_BuildError("Keyboard interrupt"))
            raise
        finally:
            self._buildable_rules.stop()
            for thread in threads:
                thread.join()
        
        if self._buildable_rules.errors:
            lines = []
            for error in self._buildable_rules.errors:
                lines.append("%s" % (error))
                if error.extra_info:
                    lines.extend(error.extra_info)
                lines.append("")
            raise _BuildError("At least one rule failed to build", lines)

        self._building = False
    
    def _run_do_later_funcs(self):
        # run do_later commands until there aren't any more
        try:
            while self.scope._do_later_funcs:
                funcs = self.scope._do_later_funcs
                self.scope._do_later_funcs = []
                for f in funcs:
                    f()
        except _BuildError:
            raise
        except Exception as e:
            raise _BuildError("Error running do_later function (in %s)" % (self.scope.dir), _get_exception_info())
    
    def _have_unbuilt(self):
        for target in self._toplevel_examined_targets:
            if not target._built:
                return True
        return False
    
    def _run_prebuild_funcs(self):
        while self._prebuild_funcs:
            funcs = self._prebuild_funcs
            self._prebuild_funcs = []
            try:
                for scope, f in funcs:
                    self._local.current_scope = scope
                    os.chdir(scope.dir)
                
                    self.scope.prepare_do_later()
                    f()
                    self._run_do_later_funcs()
                
                    recurse_dirs = self.scope.recurse_dirs
                    self.scope.recurse_dirs = set()

                    for d in recurse_dirs:
                        self._handle_dir(d)
            except _BuildError:
                raise
            except Exception as e:
                raise _BuildError("Error running prebuild function (in %s)" % (self.scope.dir), _get_exception_info())
    
    def _run_postbuild_funcs(self):
        funcs = self._postbuild_funcs
        self._postbuild_funcs = []
        try:
            for scope, f in funcs:
                self._local.current_scope = scope
                os.chdir(scope.dir)
                
                self.scope.prepare_do_later()
                f()
                self._run_do_later_funcs()
        except _BuildError:
            raise
        except Exception as e:
            raise _BuildError("Error running postbuild function (in %s)" % (self.scope.dir), _get_exception_info())
    
    def _run_module_post_functions(self):
        fname = "post_" + self.scope.scope_type
        for mod in self.scope.modules.values():
            _try_call_method(mod, fname)
    
    def _load_config(self):
        # load global config
        self.scope.prepare_do_later()
        search_paths = [os.path.join(self._emk_dir, "config")]
        self._import_from(search_paths, "emk_global", set_scope_dir=True)
        self._run_module_post_functions()
        self._run_do_later_funcs()
    
    def _load_parent_scope(self, path):
        # the immediate parent scope is stored in every directory that we visit, so we only need to recurse
        # up the directory tree until we hit a directory we have already seen (or the project dir).
        proj_dir = None
        new_subproj_dirs = []
        parent_scope = self._root_scope
        d = path
        prev = None
        while d != prev: # iterate until we hit the root directory
            if d in self._stored_subproj_scopes:
                # found a directory with a cached parent scope
                parent_scope = self._stored_subproj_scopes[d]
                break
            if os.path.isfile(os.path.join(d, "emk_subproj.py")):
                # we will need to load this subproj file later
                new_subproj_dirs.append((d, True))
            else:
                # if there is no subproj file, the scope from the parent directory will be propagated down.
                new_subproj_dirs.append((d, False))
            if os.path.isfile(os.path.join(d, "emk_project.py")):
                # found project dir, so exit
                proj_dir = d
                break
            prev = d
            d, tail = os.path.split(d)
        
        if proj_dir:
            # load emk_project.py if necessary.
            self._local.current_scope = _ScopeData(self._root_scope, "project", proj_dir, proj_dir)
            
            self._local.current_scope.prepare_do_later()
            self.import_from([proj_dir], "emk_project")
            self._run_module_post_functions()
            self._run_do_later_funcs()
            
            parent_scope = self._local.current_scope
        elif parent_scope is self._root_scope:
            # no project dir; use root directory
            proj_dir = d
        else:
            # use cached project dir
            proj_dir = parent_scope.proj_dir
        
        # Now cache the parent scope for each directory we visited.
        new_subproj_dirs.reverse()
        for d, have_subproj in new_subproj_dirs:
            if have_subproj:
                # load emk_subproj.py if necessary.
                self._local.current_scope = parent_scope = _ScopeData(parent_scope, "subproj", d, proj_dir)
            
                self.scope.prepare_do_later()
                self.import_from([d], "emk_subproj")
                self._run_module_post_functions()
                self._run_do_later_funcs()
            
                self._stored_subproj_scopes[d] = self.scope
            else:
                self._stored_subproj_scopes[d] = parent_scope
        
        self._local.current_scope = parent_scope
        self._current_proj_dir = proj_dir
    
    def _load_scope_cache(self, scope):
        if not self.cleaning:
            hash = hashlib.md5(scope.dir).hexdigest()
            cache_path = os.path.join(scope.dir, scope.build_dir, "__emk_cache__" + hash)
            try:
                with open(cache_path, "rb") as f:
                    scope._cache = pickle.load(f)
            except IOError:
                pass
        if scope._cache is None:
            scope._cache = {}

    def _remove_cache(self, cache_path):
        try:
            os.remove(cache_path)
        except OSError:
            pass

    def _write_scope_caches(self):
        if self.cleaning:
            return
        for path, scope in self._visited_dirs.items():
            hash = hashlib.md5(path).hexdigest()
            cache_path = os.path.join(path, scope.build_dir, "__emk_cache__" + hash)
            if scope._cache:
                try:
                    with open(cache_path, "wb") as f:
                        pickle.dump(scope._cache, f, -1)
                except IOError:
                    self.log.error("Failed to write cache file %s", cache_path)
                except:
                    self._remove_cache(cache_path)
                    raise
    
    def _handle_dir(self, d, first_dir=False):
        path = os.path.realpath(d)
        if path in self._visited_dirs:
            return
        
        try:
            os.chdir(path)
        except OSError:
            raise _BuildError("Failed to change to directory %s" % path)
            
        self.log.info("Entering directory %s", path)
        
        # First, load the parent scope, and create a rules scope for this directory.
        self._load_parent_scope(path)
        self._local.current_scope = _ScopeData(self._local.current_scope, "rules", path, self._current_proj_dir)
        
        self._load_scope_cache(self._local.current_scope)
        
        # Load any preload modules that have been inherited from the parent scope(s).
        self.scope.prepare_do_later()
        self.module(self.scope.pre_modules) # load preload modules
        # Try to load the emk_rules.py file. If we can't load the default modules instead (if any).
        if not self.import_from([path], "emk_rules"):
            self.module(self.scope.default_modules)
        self._run_do_later_funcs()
        
        # Run post_rules() (if present) for each module that was loaded into the scope.
        self.scope.prepare_do_later()
        self._run_module_post_functions()
        self._run_do_later_funcs()
        
        # Store build dir for this directory so we can correctly resolve the build dir placeholder later.
        self._known_build_dirs[path] = self.scope.build_dir
        
        # gather dirs to (potentially) recurse into
        recurse_dirs = self.scope.recurse_dirs
        self.scope.recurse_dirs = set()
        
        if not self._cleaning:
            try:
                os.makedirs(self.scope.build_dir)
            except OSError as e:
                if e.errno == errno.EEXIST and os.path.isdir(self.scope.build_dir):
                    pass
                else:
                    raise
        
        if first_dir:
            self._fix_explicit_targets()
            self._fix_traces()
        
        self._visited_dirs[path] = self.scope
        
        for d in recurse_dirs:
            self._handle_dir(d)
    
    def _module(self, name, weak):
        # if the module has already been loaded in the current scope, return the existing instance
        if name in self.scope.modules:
            return self.scope.modules[name].instance

        if name in self.scope.weak_modules:
            mod = self.scope.weak_modules[name]
            if not weak:
                self.scope.modules[name] = mod
            return mod.instance

        if self.building:
            stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
            raise _BuildError("Cannot load a new module when building", stack)

        # if any of the parent scopes contain an instance, return a new instance with that as a parent
        cur = self.scope.parent
        while cur:
            d = None
            if name in cur.modules:
                d = cur.modules
            elif name in cur.weak_modules:
                d = cur.weak_modules

            if d:
                try:
                    instance = d[name].instance.new_scope(self.scope_name)
                except _BuildError:
                    raise
                except Exception:
                    raise _BuildError("Error creating new scope for module %s" % (name), _get_exception_info())

                mod = _Module_Instance(name, instance, d[name].mod)
                _try_call_method(mod, "load_" + self.scope_name)
                if weak:
                    self.scope.weak_modules[name] = mod
                else:
                    self.scope.modules[name] = mod
                return instance
            cur = cur.parent

        # otherwise, try to load the module from the module paths
        oldpath = os.getcwd()
        fp = None
        try:
            fixed_module_paths = [_make_target_abspath(path, self.scope) for path in self.scope.module_paths]
            self.log.debug("Trying to load module %s from %s", name, fixed_module_paths)
            fp, pathname, description = imp.find_module(name, fixed_module_paths)
            mpath = os.path.realpath(pathname)
            if not mpath in self._all_loaded_modules:
                d, tail = os.path.split(mpath)
                os.chdir(d)
                self._all_loaded_modules[mpath] = imp.load_module(name, fp, pathname, description)

            instance = self._all_loaded_modules[mpath].Module(self.scope_name)
            mod = _Module_Instance(name, instance, self._all_loaded_modules[mpath])
            _try_call_method(mod, "load_" + self.scope_name)
            if weak:
                self.scope.weak_modules[name] = mod
            else:
                self.scope.modules[name] = mod
            return instance
        except ImportError:
            pass
        except _BuildError:
            raise
        except Exception:
            raise _BuildError("Error loading module %s" % (name), _get_exception_info())
        finally:
            os.chdir(oldpath)
            if fp:
                fp.close()

        self.log.info("Module %s not found", name)
        return None
    
    def _rule(self, func, produces, requires, args, cwd_safe, ex_safe, has_changed, stack):
        if self.scope_name != "rules":
            raise _BuildError("Cannot create rules when not in 'rules' scope (current scope = '%s')" % (self.scope_name), stack)
        
        seen_produces = set([emk.ALWAYS_BUILD])
        fixed_produces = []
        for p in _flatten_gen(produces):
            if p and p not in seen_produces:
                seen_produces.add(p)
                fixed_produces.append(p)
        fixed_requires = [_make_require_abspath(r, self.scope) for r in _flatten_gen(requires) if r != ""]
        
        if not has_changed:
            has_changed = self.default_has_changed

        new_rule = _Rule(fixed_requires, args, func, cwd_safe, ex_safe, has_changed, self.scope)
        new_rule.stack = stack
        with self._lock:
            self._rules.append(new_rule)
            for product in fixed_produces:
                new_target = _Target(product, new_rule)
                if new_target.abs_path in self._targets and self._targets[new_target.abs_path].rule:
                    lines = ["Previous rule definition:"] + ["    " + line for line in self._targets[new_target.abs_path].rule.stack]
                    lines += ["New rule definition:"] + ["    " + line for line in stack]
                    raise _BuildError("Duplicate rule producing %s" % (new_target.abs_path), lines)
                else:
                    if new_target.abs_path in self._aliases:
                        raise _BuildError("New rule produces %s, which is already an alias for %s" % (new_target.abs_path, self._aliases[new_target.abs_path]))

                    self.log.debug("Adding target %s <= %s", new_target.abs_path, fixed_requires)
                    self._targets[new_target.abs_path] = new_target
                    self.scope.targets[new_target.orig_path] = new_target
                    new_rule.produces.append(new_target)
                    self._added_rule = True
    
    def _print_bad_rules(self):
        if self._bad_rules:
            lines = []
            for rule in self._bad_rules:
                lines = [_style_tag('u') + "A rule may have been partially executed." + _style_tag('')]
                lines.append("Rule definition:")
                lines.extend(["    " + _style_tag('rule_stack') + line + _style_tag('') for line in rule.stack])
            lines.append(_style_tag('important') + "You should clean before rebuilding." + _style_tag(''))
            self.log.error('\n'.join(lines), extra={'adorn':False})
    
    def _trace_changed_str(self, s):
        if self._options["style"] == "no":
            return '*' + s + '*'
        else:
            return _style_tag('red') + s + _style_tag('')
    
    def _trace_unknown_str(self, s):
        if self._options["style"] == "no":
            return '(' + s + ')'
        else:
            return _style_tag('blue') + s + _style_tag('')
    
    def _trace_helper(self, rule, visited, to_visit):
        if not rule:
            return
        
        strings = []
        if rule._built:
            for target, changed in rule._req_trace.items():
                path = target.abs_path
                if changed:
                    strings.append(self._trace_changed_str(path))
                    if target not in visited:
                        to_visit.append(target)
                elif changed is not None:
                    strings.append(path)
                    if self._trace_unchanged and target.rule not in visited:
                        to_visit.append(target)
        else:
            for req, weak in rule._required_targets:
                path = req.abs_path
                if path is self.ALWAYS_BUILD:
                    strings.append(self._trace_changed_str(path))
                else:
                    strings.append(self._trace_unknown_str(path))
                    if req.rule not in visited:
                        to_visit.append(req)
        
        if rule._ran_func:
            s = ", ".join([self._trace_changed_str(p.abs_path) for p in rule.produces])
        else:
            s = ", ".join([p.abs_path for p in rule.produces])
        if strings:
            s = s + " <= " + ", ".join(strings)
        else:
            s = s + " <= (none)" 
        self.log.info(s)
    
    def _do_trace(self, target):
        self.log.info("")
        self.log.info(_style_tag('u') + "Trace for %s" % (target.abs_path) + _style_tag(''))
        
        if target._required_by:
            self.log.info("Rules that require %s:" % (target.abs_path))
            for r in target._required_by:
                strings = [t.abs_path for t, weak in r._required_targets]
                s = ", ".join([p.abs_path for p in r.produces])
                if strings:
                    s = s + " <= " + ", ".join(strings)
                else:
                    s = s + " <= (none)"
                self.log.info("  " + s)
        else:
            self.log.info("There are no rules that require %s" % (target.abs_path))
        
        self.log.info(_style_tag('bold') + "Dependency trace for %s:" % (target.abs_path) + _style_tag(''))
        if self._options["style"] == "no":
            self.log.info("Changed files (or files for which there was no cached info) are indicated by *<file>*")
            self.log.info("Files that were not examined (due to a build error) are indicated by (<file>)")
        else:
            self.log.info("Changed files (or files for which there was no cached info) are in " + _style_tag('red') + "red" + _style_tag(''))
            self.log.info("Files that were not examined (due to a build error) are in " + _style_tag('blue') + "blue" + _style_tag(''))
        
        visited = set()
        to_visit = collections.deque()
        to_visit.append(target)
        while to_visit:
            next = to_visit.popleft()
            if next.rule not in visited:
                visited.add(next.rule)
                self._trace_helper(next.rule, visited, to_visit)
    
    def _print_traces(self):
        for trace_target in self.traces:
            t = self._get_target(trace_target)
            if t:
                self._do_trace(t)
            else:
                self.log.info("")
                self.log.info("Could not trace '%s' since there is no rule to build it." % (trace_target))
            pass
        if self.traces:
            self.log.info("")

class EMK(EMK_Base):
    """
    The public emk API.
    
    The module-level setup() function (called by main()) installs an instance of this class into builtins named 'emk'.
    Therefore you can access this instance from within your emk_global.py, emk_project.py, emk_subproj.py, or emk_rules.py
    files without importing. For example, you can use emk.build_dir to get the relative build directory for the current scope.
    
    Classes:
      BuildError -- Exception type raised when a build error occurs.
      Target     -- Representation of a potential target (ie, a rule product).
      Rule       -- Representation of a rule.
    
    Global read-only properties (not based on current scope):
      log              -- The emk log (named 'emk'). Modules should create sub-logs of this to use the emk logging features.
      formatter        -- The formatter instance for the emk log.
      
      ALWAYS_BUILD     -- A special token. When used as a rule requirement, ensures that the rule will always be executed.
      
      cleaning         -- True if "clean" has been passed as an explicit target; false otherwise.
      building         -- True when rules are being executed, false at other times.
      emk_dir          -- The directory which contains the emk module.
      options          -- A dict containing all command-line options passed to emk (ie, arguments of the form key=value).
                          You can modify the contents of this dict.
      explicit_targets -- The set of explicit targets passed to emk (ie, all arguments that are not options).
      traces           -- The set of targets that will be traced once the build is complete (for debugging).
    
    Global modifiable properties:
      default_has_changed   -- The default function to determine if a rule requirement or product has changed. If replaced, the replacement
                               function should take a single argument which is the absolute path of the thing to check to see if it has changed.
                               When this function is executing, emk.current_rule and emk.rule_cache() are available.
      build_dir_placeholder -- The placeholder to use for emk.build_dir in paths passed to emk functions. The default value is "$:build:$".
      proj_dir_placeholder  -- The placeholder to use for emk.proj_dir in paths passed to emk functions. The default value is "$:proj:$".
    
    Scoped read-only properties (apply only to the current scope):
      scope_name    -- The name of the current scope. May be one of ['global', 'project', 'subproj', 'rules].
      proj_dir      -- The absolute path of the project directory for the current scope.
      scope_dir     -- The absolute path of the directory in which the scope was created
                       (eg, the directory from which the emk_<scope name>.py file was loaded).
      local_targets -- The dict of potential targets (ie, rule products) defined in the current scope. This maps the original target path
                       (ie, as passed into emk.rule() or @emk.make_rule) to the emk.Target instance.
      current_rule  -- The currently executing rule (an emk.Rule instance), or None if a rule is not being executed.
      
    Scoped modifiable properties (inherited by child scopes):
      build_dir       -- The build directory path (may be relative or absolute). The default value is "__build__".
      module_paths    -- Additional absolute paths to search for modules.
      default_modules -- Modes that are loaded if no emk_rules.py file is present.
      pre_modules     -- Modules that are preloaded before each emk_rules.py file is loaded.
    """
    
    def __init__(self, args):
        super(EMK, self).__init__(args)
        
        self.BuildError = _BuildError
        self.Target = _Target
        self.Rule = _Rule
        self.Container = _Container

    def _set_build_dir(self, dir):
        if self.scope.scope_type == "rules":
            stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
            raise _BuildError("Cannot change the build dir when in rules scope", stack)
        self.scope.build_dir = dir

    def _set_module_paths(self, paths):
        self.scope.module_paths = paths

    def _set_default_modules(self, names):
        self.scope.default_modules = names

    def _set_pre_modules(self, names):
        self.scope.pre_modules = names

    cleaning = property(lambda self: self._cleaning)
    building = property(lambda self: self._building)
    
    emk_dir = property(lambda self: self._emk_dir)
    options = property(lambda self: self._options)
    explicit_targets = property(lambda self: self._explicit_targets)
    
    scope_name = property(lambda self: self.scope.scope_type)
    proj_dir = property(lambda self: self.scope.proj_dir)
    scope_dir = property(lambda self: self.scope.dir)
    build_dir = property(lambda self: self.scope.build_dir, _set_build_dir)
    module_paths = property(lambda self: self.scope.module_paths, _set_module_paths)
    default_modules = property(lambda self: self.scope.default_modules, _set_default_modules)
    pre_modules = property(lambda self: self.scope.pre_modules, _set_pre_modules)
    
    local_targets = property(lambda self: self.scope.targets)
    
    current_rule = property(lambda self: self._local.current_rule)
    
    def run(self, path):
        """
        Run the emk build in the given directory.
        
        The build process in a given directory goes as follows:
          1. Load the global emk config from <emk dir>/config/emk_global.py>, if it exists and has not
              already been loaded (creates the root scope).
          2. Find the project dir. The project dir is the closest ancestor to the current directory that
              contains an "emk_project.py" file, or the root directory if no project file is found.
          3. Load the project file "emk_project.py" from the project dir if it exists and has not already been loaded (creates a new scope).
          4. For each directory between the project dir and the current dir, load "emk_subproj.py" from that directory
              if it exists and has not already been loaded (creates a new scope).
          5. Create the rules scope for the current directory.
          6. Load any premodules.
          7. Load "emk_rules.py" from the current directory if it exists; otherwise, load the default modules (if any).
          8. Run module post_rules() methods.
          9. Recurse into any specified dirs that have not already been visited.
        
        Once there are no more directories to recurse into, the prebuild functions are executed until there aren't any more.
        Prebuild functions specified during the prebuild stage are executed after all of the previous prebuild functions
        have been executed.
        
        Then, the first build phase starts. If explicit targets have been specified and they can all be resolved, only those
        targets (and their dependencies) are examined. Otherwise, all autobuild targets (and their dependencies) are examined.
        The rule that produces each examined target will be executed if the dependencies have changed (or if the products have changed
        and have been declared as rebuild_if_changed). Target examination will proceed through the dependency tree until it reaches
        dependencies that exist and have no rule to make them (ie, normal files that are not generated as part of the build process).
        Rules are executed in dependency order, so dependencies are built before the things that depend on them (as you would expect).
        There is no ordering between rules with no dependency relationship.
        
        Building continues until everything that can be built (from the set of examined targets) has been built. Note that it is
        possible that not all examined targets could be built immediately, since they may depend on things for which rules have
        not yet been declared. emk will attempt to build those targets later.
        
        Once building is complete, the postbuild functions are executed. Note that if new postbuild functions are added during
        the postbuild stage, they will not be executed until after the next build phase.
        
        Finally, any new directories are recursed into. If there is still work left to do (ie, unbuilt targets), emk will start
        a new build phase (returning to the prebuild stage). Build phases will continue until all targets are built, or until
        there is nothing left to do. If there are unbuilt targets after building has stopped, a build error is raised.
        
        Arguments:
          path -- The directory to start the build process from.
        """
        if self._did_run:
            stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
            raise _BuildError("Cannot call run() again", stack)
        self._did_run = True
        
        self.log.info("Using %d %s", self._build_threads, ("thread" if self._build_threads == 1 else "threads"))
        
        path = os.path.realpath(path)
        root_scope = _ScopeData(None, "global", path, _find_project_dir(path))
        root_scope.module_paths.append(os.path.join(self._emk_dir, "modules"))
        self._root_scope = root_scope
        self._local.current_scope = root_scope
        
        # insert "clean" module
        self.insert_module("clean", _Clean_Module(self.scope_name))
        self.pre_modules.append("clean")
        
        self._time_lines = []
        self._build_phase = 1
        
        try:
            self._load_config()
            
            start_time = time.time()
            phase_start_time = start_time
            self._handle_dir(path, first_dir=True)

            self._done_build = False
            while ((self._have_unbuilt() or self._explicit_targets) and (self._added_rule or self._prebuild_funcs or self._postbuild_funcs)) or \
              self._must_build or \
              ((not self._done_build) and (self._auto_targets or self._prebuild_funcs or self._postbuild_funcs)):
                self._run_prebuild_funcs()
            
                self._remove_artificial_targets()
                self._fix_aliases()
                self._fix_depends()
                self._fix_weak_depends()
                for rule in self._rules:
                    self._fix_requires(rule)
                self._fix_attached()
                self._fix_auto_targets()
                self._fix_requires_rule()
                self._fix_rebuild_if_changed()

                self._added_rule = False
            
                self._do_build()
                self._must_build = []
            
                self._run_postbuild_funcs()
                
                self.log.debug("**** End of phase %d ****", self._build_phase)
                
                now = time.time()
                self._time_lines.append("Phase %d: %0.3f seconds" % (self._build_phase, now - phase_start_time))
                phase_start_time = now
                self._build_phase += 1
            
                # recurse into any new dirs
                for scope in self._visited_dirs.values():
                    if scope.recurse_dirs:
                        recurse_dirs = scope.recurse_dirs
                        scope.recurse_dirs = set()
                    
                        self._local.current_scope = scope.parent
                        for d in recurse_dirs:
                            self._handle_dir(d)
                    
        finally:
            self._write_scope_caches()
        
        unbuilt = set()
        for path, target in self._targets.items():
            if target._visited and not target._built:
                unbuilt.add(target)
        
        unbuilt_lines = []
        for target in unbuilt:
            if target.rule:
                unbuilt_deps = []
                for dep, weak in target.rule._required_targets:
                    if (dep.rule or not weak) and (dep in unbuilt):
                        unbuilt_deps.append(dep.abs_path)
                unbuilt_lines.append("%s depends on unbuilt %s" % (target.abs_path, unbuilt_deps))
            else:
                unbuilt_lines.append("No rule produces %s, and it does not exist" % (target.abs_path))
        if unbuilt:
            raise _BuildError("Some targets could not be built", unbuilt_lines)
        
        if self._explicit_targets:
            raise _BuildError("No rule creates these explicitly specified targets:", self._explicit_targets)
        
        for line in self._time_lines:
            self.log.debug(line)
        diff = time.time() - start_time
        self.log.info("Finished in %0.3f seconds" % (diff))
    
    def import_from(self, paths, name):
        """
        Import a Python module from a set of search directories.
        
        Finds the module in the given search directories using imp.find_module(). If found, emk will change
        the working directory to the directory that the module was found in, import the module, and then
        return the working directory to its original state. If the module is not found, no error is raised,
        but None will be returned.
        
        Arguments:
          paths -- A list of paths (relative or absolute) to search for the module in.
          name  -- The name of the module to load.
        
        Returns the loaded Python module, or None if the module could not be found. Raises a BuildError if
        an exception occurs while importing the module.
        """
        return self._import_from(paths, name)
    
    def module(self, *names):
        """
        Load one or more emk modules into the current scope.
        
        emk has a module system which allows automatic creation of rules, and easy hierarchical configuration.
        When a module is loaded into a scope, emk will check to see if the module is already present in the scope;
        if it is, then the module instance is returned. Otherwise, emk will try to find an instance of the module
        in a parent scope. If a parent instance is found, a new instance is created for the current scope using the
        parent instance's new_scope() method. This allows the new module instance to inherit configuration values
        from the parent scope if desired (based on how the module was designed).
        
        If the module is not present in any parent scope, emk will try to load a Python module of the same name from
        the scope's module search paths (emk.module_paths). Note that the module search paths may be relative;
        relative paths and project/build dir placeholders are replaced based on the current scope. If the Python
        module is found, it is imported (if it was not previously imported), with the current working directory
        set to the directory that the Python module was found in. An emk module instance is created by calling
        Module(<current scope name>) on the Python module instance. This can be any callable that returns an
        emk module instance, but is usually a class named Module.
        
        An emk module instance must provide a new_scope() method that takes the new scope type, and returns an
        emk module instance (potentially the same module instance; it is not required to create a new module instance).
        In addition, a module instance may provide load_* or post_* methods, where * may be any of the scope types
        ('global', 'project', 'subproj', or 'rules'). These methods should take no arguments. The load_* method is called
        when a new module instance is loaded into a scope of the corresponding type (after the new instance is created).
        The post_* method is called after the corresponding scope has been fully loaded (eg, after the emk_rules.py file
        has been imported for the rules scope).
        
        Modules should only add emk rules in the post_rules method (or later, if the post_rules method uses emk.do_later(),
        emk.prebuild(), or emk.postbuild()).
        
        It is advisable to avoid having a circular dependency between emk modules (if the modules load each other at import
        time or when the module isntance is created) since this will probably lead to an infinite loop.
        
        Arguments:
          names -- The list of modules names (or a single name) to load into the current scope.
        
        Returns the list of module instances corresponding to the given module names; None will be in the list for each module
        that could not be loaded. If only one name is provided, the result will be a value rather than a list (for convenience,
        so that you can write 'mymod = emk.module("my_module")', but also write 'c, link = emk.module("c", "link")').
        """
        mods = []
        for name in _flatten_gen(names):
            mods.append(self._module(name, weak=False))
        if len(mods) == 1:
            return mods[0]
        return mods

    def weak_module(self, *names):
        """
        Load one or emk modules into the current scope, without causing their post_<scope type>() methods to be called.
        
        This is to provide a way to modify the configuration of a module for child scopes, without having each module's
        post_<scope type>() method called (so the module should not create any rules).
        
        Any modules that are also loaded normally (using emk.module()) at any point will have their post_<scope type>()
        methods called as usual.
        
        Arguments:
          names -- The list of modules names (or a single name) to load into the current scope as weak modules.
        
        Returns the list of module instances corresponding to the given module names; None will be in the list for each module
        that could not be loaded. If only one name is provided, the result will be a value rather than a list (for convenience,
        so that you can write 'mymod = emk.module("my_module")', but also write 'c, link = emk.module("c", "link")').
        """
        mods = []
        for name in _flatten_gen(names):
            mods.append(self._module(name, weak=True))
        if len(mods) == 1:
            return mods[0]
        return mods
    
    def insert_module(self, name, instance):
        """
        Insert an emk module instance into the current scope as a weak module.
        
        This method allows you to create a module instance and provide it for use by child scopes without needing to
        create an actual Python module file to import. The instance will be installed into the current scope as a weak
        module, so the current scope can also load it using emk.module() if desired after it has been inserted.
        
        When the module instance is being inserted, its load_<scope type>() method will be called, if present. If a module
        instance of the same name already exists in the current scope (either as a normal module or weak module), a build
        error will be raised; however you can insert a module that will override a module in any parent scope (or a Python module)
        as long as the current scope has not yet loaded it.
        
        Arguments:
          name     -- The name of the module being inserted (as would be passed to emk.module())
          instance -- The module instance to insert.
        
        Returns:
          The inserted module instance, or None if it could not be inserted.
        """
        stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
        if self.building:
            raise _BuildError("Cannot call insert_module() when building", stack)

        if name in self.scope.modules or name in self.scope.weak_modules:
            raise _BuildError("Cannot insert '%s' module since it has already been loaded into the current scope", stack)

        mod = _Module_Instance(name, instance, None)
        _try_call_method(mod, "load_" + self.scope_name)
        self.scope.weak_modules[name] = mod
        return instance
    
    def rule(self, func, produces, requires, *args, **kwargs):
        """
        Declare an emk rule.
        
        Any function that takes at least two arguments (the list of product paths and the list of requirement paths)
        can be used in an emk rule. When the function is executed, it must ensure that all declared products are actually
        produced (they must be either present in the filesystem, or declared virtual using emk.mark_virtual()). emk will
        ensure that all the requirements in the requires list (the primary dependencies) have been built or otherwise exist
        before the rule function is executed.
        
        Rules may be declared as either cwd-safe or cwd-unsafe (using the cwd_safe keyword argument).
        cwd-safe rules may be executed in parallel and must not depend on the current working directory.
        cwd-unsafe rules are all executed by a single thread; the current working directory will be set to
        the scope directory that the rule was created in (eg, the directory containing emk_rules.py) before the rule is executed.
        
        It is a build error to declare more than one rule that produces the same target.
        
        Arguments:
          func     -- The rule function to execute. Must take the correct number of arguments (produces, requires, and the additional args).
          produces -- List of paths that the rule produces. The paths may be absolute, or relative to the scope dir.
                      Project and build dir placeholders will be resolved according to the current scope. Empty paths ("") are ignored.
                      This argument will be converted into a list of canonical paths, and passed as the first argument to the rule function.
          requires -- List of paths that the rule requires to be built before it can be executed (ie, dependencies).
                      The paths may be absolute, or relative to the scope dir. Project and build dir placeholders will
                      be resolved according to each path. Empty paths ("") are ignored. May include the special
                      emk.ALWAYS_BUILD token to indicate that the rule should always be executed.
                      This argument will be converted into a list of canonical paths, and passed as the second argument to the rule function.
          args     -- Additional arguments that will be passed to the rule function.
          kwargs   -- Keyword arguments - see below.
        
        Keyword arguments:
          cwd_safe    -- If True, the rule is considered to be cwd-safe (ie, does not depend on the current working directory).
                         The default value is False.
          ex_safe     -- If False, then emk will print a warning message if the execution of the rule is interrupted in any way.
                         The warning indicates that the rule was partially executed and may have left partial build products, so
                         the build should be cleaned. The default value is False.
          has_changed -- The function to execute for this rule to determine if the dependencies (or "rebuild if changed" products)
                         have changed. The default value is emk.default_has_changed.
        """
        stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
        cwd_safe = kwargs.get("cwd_safe", False)
        ex_safe = kwargs.get("ex_safe", False)
        has_changed = kwargs.get("has_changed", None)
        self._rule(func, produces, requires, args, cwd_safe, ex_safe, has_changed, stack)
    
    def make_rule(self, produces, requires, *args, **kwargs):
        """
        Decorator to turn a function into an emk rule.
        
        Any function that takes at least two arguments (the list of product paths and the list of requirement paths)
        can be turned into an emk rule using @emk.make_rule(). The functionality is the same as passing 
        the decorated function as the first argument to emk.rule(), with all other arguments being the same as those
        passed to the @emk.make_rule() decorator.
        
        Arguments:
          produces -- List of paths that the rule produces. The paths may be absolute, or relative to the scope dir.
                      Project and build dir placeholders will be resolved according to the current scope. Empty paths ("") are ignored.
                      This argument will be converted into a list of canonical paths, and passed as the first argument to the rule function.
          requires -- List of paths that the rule requires to be built before it can be executed (ie, dependencies).
                      The paths may be absolute, or relative to the scope dir. Project and build dir placeholders will
                      be resolved according to each path. Empty paths ("") are ignored. May include the special
                      emk.ALWAYS_BUILD token to indicate that the rule should always be executed.
                      This argument will be converted into a list of canonical paths, and passed as the second argument to the rule function.
          args     -- Additional arguments that will be passed to the rule function.
          kwargs   -- Keyword arguments - see below.
        
        Keyword arguments:
          cwd_safe    -- If True, the rule is considered to be cwd-safe (ie, does not depend on the current working directory).
                         The default value is False.
          ex_safe     -- If False, then emk will print a warning message if the execution of the rule is interrupted in any way.
                         The warning indicates that the rule was partially executed and may have left partial build products, so
                         the build should be cleaned. The default value is False.
          has_changed -- The function to execute for this rule to determine if the dependencies (or "rebuild if changed" products)
                         have changed. The default value is emk.default_has_changed.
        """
        def decorate(func):
            stack = _format_decorator_stack(_filter_stack(traceback.extract_stack()[:-1]))
            cwd_safe = kwargs.get("cwd_safe", False)
            ex_safe = kwargs.get("ex_safe", False)
            has_changed = kwargs.get("has_changed", None)
            self._rule(func, produces, requires, args, cwd_safe, ex_safe, has_changed, stack)
            return func
        return decorate
    
    def depend(self, target, *dependencies):
        """
        Add secondary dependencies to a target.
        
        If emk determines that a target needs to be built, it will examine the dependencies of the rule that produces
        that target. The primary dependencies are defined by the "requires" argument when the rule is created. The
        secondary dependencies of the rule are the set of secondary dependencies of all products of that rule, which are
        added using emk.depend(). All primary and secondary dependencies of a rule are built by emk before the rule is executed.
        
        Arguments:
          target       -- The target path to add secondary dependencies for. The path may be absolute, or relative to the scope dir.
                          Project and build dir placeholders will be resolved according to the current scope.
          dependencies -- The secondary dependency paths. The paths may be absolute, or relative to the scope dir.
                          Project and build dir placeholders will be resolved based on each path.
        """
        fixed_depends = [_make_require_abspath(d, self.scope) for d in _flatten_gen(dependencies) if d != ""]
        if not fixed_depends:
            return
        
        abspath = _make_target_abspath(target, self.scope)
        self.log.debug("Adding %s as dependencies of target %s", fixed_depends, abspath)
        with self._lock:
            if abspath in self._secondary_dependencies:
                self._secondary_dependencies[abspath].extend(fixed_depends)
            else:
                self._secondary_dependencies[abspath] = list(fixed_depends)
    
    def weak_depend(self, target, *dependencies):
        """
        Add weak secondary dependencies to a target.
        
        Weak secondary dependencies are treated like normal secondary dependencies, except that
        if a weak secondary dependency does not exist, the rule may still be executed, and no build error
        is raised.
        
        This functionality is intended for dependencies that are discovered through examination of the
        primary dependencies. The driving example is header files for C/C++.
        
        Arguments:
          target       -- The target path to add weak dependencies for. The path may be absolute, or relative to the scope dir.
                          Project and build dir placeholders will be resolved according to the current scope.
          dependencies -- The weak dependency paths. The paths may be absolute, or relative to the scope dir.
                          Project and build dir placeholders will be resolved based on each path.
        """
        fixed_depends = [_make_require_abspath(d, self.scope) for d in _flatten_gen(dependencies) if d != ""]
        if not fixed_depends:
            return

        abspath = _make_target_abspath(target, self.scope)
        self.log.debug("Adding %s as weak dependencies of target %s", fixed_depends, abspath)
        with self._lock:
            if abspath in self._weak_dependencies:
                self._weak_dependencies[abspath].extend(fixed_depends)
            else:
                self._weak_dependencies[abspath] = list(fixed_depends)
    
    def attach(self, target, *attached_targets):
        """
        Specify a set of targets that must be built if the given attach target is built.
        
        This allows you to ensure that if one target is built, the attached targets will also be built.
        It does not imply any sort of dependency between the targets; the build order of the targets is not affected.
        
        You may attach to a target that does not yet exist; you can also attach to a target that has already been built.
        
        Arguments:
          target           -- The target path to attach to. The path may be absolute, or relative to the scope dir.
                              Project and build dir placeholders will be resolved according to the current scope.
          attached_targets -- The target paths that must be built if <target> is built. The paths may be absolute, or relative to
                              the scope dir. Project and build dir placeholders will be resolved based on each path.
        """
        fixed_depends = [_make_require_abspath(d, self.scope) for d in _flatten_gen(attached_targets) if d != ""]
        abspath = _make_target_abspath(target, self.scope)
        self.log.debug("Attaching %s to target %s", fixed_depends, abspath)
        with self._lock:
            if abspath in self._attached_dependencies:
                self._attached_dependencies[abspath].extend(fixed_depends)
            else:
                self._attached_dependencies[abspath] = list(fixed_depends)
    
    def autobuild(self, *targets):
        """
        Mark the given targets as autobuild.
        
        If no explicit targets are passed in on the command line, emk will build all targets that have been
        marked as atuobuild. emk will also build all autobuild targets when the explicit targets cannot be
        fully built due to missing rules or dependencies.
        
        Arguments:
          targets -- The target paths to mark as autobuild. The paths may be absolute, or relative to the scope dir.
                     Project and build dir placeholders will be resolved according to the current scope.
        """
        with self._lock:
            for target in _flatten_gen(targets):
                self.log.debug("Marking %s for automatic build", target)
                self._auto_targets.add(_make_target_abspath(target, self.scope))
    
    def alias(self, target, alias):
        """
        Create an alias for a given target.
        
        This allows the target to be referred to by the alias path, potentially making it easier to specify
        on the command line, or as a dependency.

        If there is an existing alias with the same canonical path, or a rule is ever declared to produce a target
        with the same path, a build error will be raised.
        
        Aliases may refer to other aliases. Aliases may also refer to normal files that are not products of any rule.
        
        Arguments:
          target -- The target path to make an alias for. The path may be absolute, or relative to the scope dir.
                    Project and build dir placeholders will be resolved according to the current scope.
          alias  -- The alias path to create for the target. The path may be absolute, or relative to the scope dir.
                    Project and build dir placeholders will be resolved according to the current scope.
        """
        abs_target = _make_target_abspath(target, self.scope)
        abs_alias = _make_target_abspath(alias, self.scope)
        with self._lock:
            if abs_alias in self._aliases:
                stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
                raise _BuildError("Duplicate alias %s" % (abs_alias), stack)
            
            self.log.debug("Adding alias %s for %s", abs_alias, abs_target)
            self._aliases[abs_alias] = abs_target
            self._added_rule = True
    
    def require_rule(self, *paths):
        """
        Mark the given paths so that they must be produced by a rule (ie, cannot just exist on the filesystem).
        
        If there is more than 1 build phase, a rule in phase 1 might be defined as depending on things that are only produced
        by rules in phase 2 or later (for example). Normally, if a rule depends on a file with no rule to make it, the rule
        can be run as long as the file exists. However, if that file would be updated by a rule defined in a later build phase,
        the rule that depends on it should not be run until after that later rule has been defined (and executed, if required).
        
        By requiring those dependencies to be produced by a rule, the build process will execute correctly - emk will wait
        until the later build phase has defined and executed the rule(s) that produce the dependencies before examining
        the rules that depend on them.
        
        Arguments:
          paths -- The list of rule product paths to mark as requiring a rule. The paths may be absolute, or relative to
                   the scope dir. Project and build dir placeholders will be resolved based on each path.
        """
        with self._lock:
            for path in _flatten_gen(paths):
                abs_path = _make_require_abspath(path, self.scope)
                self.log.debug("Requiring %s to be built by an explicit rule", abs_path)
                self._requires_rule.add(abs_path)
    
    def rebuild_if_changed(self, *paths):
        """
        Mark the given rule products so that the rule is re-executed if those products have changed,
        even if the rule's dependencies have not changed.
        
        This can be used to ensure that after a build is complete, the given products are always the correct
        output of the rule, even if the products have been modified after the previous build.
        
        Arguments:
          paths -- The list of rule product paths to mark as "rebuild if changed". The paths may be absolute, or
                   relative to the scope dir. Project and build dir placeholders will be resolved based on each path.
        """
        with self._lock:
            for path in _flatten_gen(paths):
                abs_path = _make_require_abspath(path, self.scope)
                self.log.debug("Requiring %s to be rebuilt if it has changed", abs_path)
                self._rebuild_if_changed.add(abs_path)
    
    def trace(self, *paths):
        """
        Indicate that the given targets should be traced for debugging purposes. The trace will be printed
        after the build is complete.
        
        Arguments:
          paths -- The list of target paths to trace. The paths may be absolute, or relative to the scope dir.
                   Project and build dir placeholders will be resolved according to the current scope.
        """
        with self._lock:
            for path in _flatten_gen(paths):
                abs_path = _make_target_abspath(path, self.scope)
                self.log.debug("Adding trace for %s", abs_path)
                self.traces.add(abs_path)
    
    def recurse(self, *paths):
        """
        Specify other directories for emk to visit.
        
        At any time, you may call emk.recurse(path, ...) to specify other directories for emk to visit.
        Directories that have already been visited will be ignored (based on the canonical path of the directory).
        The process for handling a directory os described in emk.run().
        
        If emk.recurse() is called when in global, project, or subproj scope, the recurse paths will be inherited
        by child scopes. Recurse paths will be visited once they are inherited by a rules scope.
        
        If emk.recurse() is called when handling a directory, the specified directories will be handled after
        the current directory has been completely handled. If emk.recurse() is called during the prebuild stage,
        the directories will be handled after the current prebuild functions have been executed. If emk.recuse()
        is called at any other time, the specified directories will be handled at the start of the next build phase
        (after the postbuild functions have been executed).
        
        Arguments:
          paths -- The list of directories to visit. The paths may be absolute, or relative to the scope dir.
                   Project and build dir placeholders will be resolved according to the current scope.
        """
        for path in _flatten_gen(paths):
            abspath = _make_target_abspath(path, self.scope)
            self.log.debug("Adding recurse directory %s", abspath)
            self.scope.recurse_dirs.add(abspath)
    
    def subdir(self, *paths):
        """
        Specify directories to recurse into, and to be cleaned when the current directory is cleaned.
        
        This is a convenience function that calls emk.recurse() on the specifed directory paths, and sets it up so
        that if "emk clean" is called in the current directory, the clean rules in the specified directories
        will also be executed.
        
        Arguments:
          paths -- The list of directory paths. The paths may be absolute, or relative to the scope dir.
                   Project and build dir placeholders will be resolved according to the current scope.
        """
        self.recurse(paths)
        sub_cleans = [os.path.join(path, "clean") for path in _flatten_gen(paths)]
        self.attach("clean", *sub_cleans)
    
    def do_later(self, func):
        """
        Specify a function to execute later in the current build stage. Cannot be called when executing a rule.
        
        Functions specified with emk.do_later() are executed at the following points in the build process:
          * After an emk_global.py, emk_project.py, or emk_subproj.py file has been imported, and any
            module post_* functions have been executed.
          * After an emk_rules.py file has been imported, before and module post_rules() functions have been executed.
          * After all module post_rules() functions have been executed for a given emk_rules.py file.
          * After each prebuild or postbuild function.
        If emk.do_later() is called while executing a do_later function, the specified function will be executed after
        all current do_later functions have been executed.
        
        Arguments:
          func -- The function to execute "later".
        """
        if self.building:
            stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
            raise _BuildError("Cannot call do_later() when building", stack)
            
        self.scope._do_later_funcs.append(func)
    
    def do_prebuild(self, func):
        """
        Specify a function to execute during the prebuild stage (see emk.run() for a description of the build stages).
        
        Prebuild functions are executed after all of the directories that are recursed into have been handled, but before
        actual building (rule execution) begins. If you specify a prebuild function during the prebuild stage, it will be
        executed after all of the currently pending prebuild functions have been executed.
        
        Arguments:
          func -- The function to execute.
        """
        with self._lock:
            self._prebuild_funcs.append((self.scope, func))
    
    def do_postbuild(self, func):
        """
        Specify a function to execute during the postbuild stage (see emk.run() for a description of the build stages).
        
        Postbuild functions are executed after the build stage of the current build phase (ie, after all rules that could be
        built (and needed to be) are examined and executed if necessary). If you specify a postbuild function during the 
        postbuild stage, it will be executed in the next build phase (ie, after the next set of rules are examined).
        
        Arguments:
          func -- The function to execute.
        """
        with self._lock:
            self._postbuild_funcs.append((self.scope, func))
    
    def mark_virtual(self, *paths):
        """
        Mark the given paths as virtual. May only be called when a rule is executing.
        
        After a rule is executed, emk checks to ensure that the rule has generated all of its declared products.
        Products that were marked as virtual by the rule are not expected to exist as actual files. All non-virtual
        products must exist in the filesystem.
        
        Arguments:
          paths -- The list of paths to mark as virtual. The paths may be absolute, or relative to the scope dir.
                   Project and build dir placeholders will be resolved according to the rule scope.
        """
        rule = self.current_rule
        if not rule:
            self.log.warning("Cannot mark anything as virtual when not in a rule")
            return
        
        abs_paths = set([_make_target_abspath(path, self.scope) for path in _flatten_gen(paths)])
        cache = rule._cache
        for path in abs_paths:
            tcache = cache.setdefault(path, {})
            self.log.debug("Marking %s as virtual", path)
            tcache["virtual"] = True
    
    def mark_untouched(self, *paths):
        """
        Mark the given paths as untouched. May only be called when a rule is executing.
        
        As a rule is executing, it may discover that some or all of its products do not actually need to be updated.
        In this case, the rule should mark those products as untouched. This will prevent unnecessary execution of
        rules that depend on the unmodified products.
        
        Note that this currently is only required for virtual products; for real products, you can achieve the same
        effect by not modifying the product.
        
        Arguments:
          paths -- The list of paths to mark as untouched. The paths may be absolute, or relative to the scope dir.
                   Project and build dir placeholders will be resolved according to the rule scope.
        """
        if not self.current_rule:
            self.log.warning("Cannot mark anything as untouched when not in a rule")
            return
            
        untouched_set = self.current_rule._untouched
        for path in _flatten_gen(paths):
            abs_path = _make_target_abspath(path, self.scope)
            self.log.debug("Marking %s as untouched", abs_path)
            untouched_set.add(abs_path)
    
    def scope_cache(self, key):
        """
        Retrieve the generic cache for a given key string in the current scope. This cache is kept separate from the rule cache.
        
        The cache can be used to store information between emk invocations. The cache can only be retrieved and modified
        when you are in rules scope (since the cache is scope-specific).
        
        Arguments:
          key -- The key string to retrieve the cache for.
        
        Returns the cache dict for the given key (or an empty dict if there was currently no cache for that key).
        Returns None if called while not in rules scope.
        """
        if self.scope._cache is None:
            return None
        return self.scope._cache.setdefault("other", {}).setdefault(key, {})
    
    def rule_cache(self, key):
        """
        Retrieve the cache for a given key string for the currently executing rule.
        
        The rule cache can be used to store information between rule invocations. The cache can only be retrieved
        when a rule is executing. The returned cache is a dict that can be modified to store data for the next time
        the rule is run. This could be used (for example) to store information to determine whether a product needs
        to be updated or can be marked 'untouched'.
        
        Arguments:
          key -- The key string to retrieve the cache for.
        
        Returns the cache dict for the given key (or an empty dict if there was currently no cache for that key).
        Returns None if there is no currently executing rule.
        """
        rule = self.current_rule
        if rule:
            return rule._cache.setdefault(key, {})
        return None
    
    def abspath(self, path):
        """
        Convert a path into an absolute path based on the current scope.
        
        When a rule is executing, the current working directory of the process will not necessarily be the
        directory that the rule was defined in (if the rule was declared to be cwd_safe). However, the directory
        that the rule was defined in is always available via emk.scope_dir. The abspath() method uses the scope dir
        to convert the given path into an absolute path, if it was not already absolute.
        
        Will convert the project and build dir placeholders ("$:proj:$" and "$:build:$", by default) with the current
        scope's project and build dirs.
        
        Arguments:
          path -- The path to convert to an absolute path.
        
        Returns the path in absolute form, relative to the scope dir.
        """
        return _make_target_abspath(path, self.scope)
    
    def fix_stack(self, stack):
        """
        Filter and format a stack trace to remove emk or threading frames from the start.
    
        Arguments:
          stack -- The stack trace to fix; should be from traceback.extract_stack() or traceback.extract_tb().
        
        Returns the formatted stack as a list of strings.
        """
        return _format_stack(_filter_stack(stack))

    def style_tag(self, tag):
        """
        Return a style tag string for the given tag.
        
        The emk log system uses tags to mark up the log output. Different stylers have different effects on the final output.
        The "no" styler translates all tags into ''.
        The "passthrough" styler does not modify tags in any way (useful if emk is calling itself).
        The HTML styler accepts any tag, since the tags are just converted to <span class='tagname'>.
        The console styler currently recognizes the following tags:
          'bold'       -- Bold/bright text.
          'u'          -- Underline.
          'red'        -- Red text.
          'green'      -- Green text.
          'blue'       -- Blue text.
          'important'  -- Bold and red text.
          'rule_stack' -- Blue text.
          'stderr'     -- Red text.
        Tag mappings can me modified/added to the console styler by modifying its self.styles dict.
        For example, "emk.formatter.styler.styles['blink'] = '\033[5m'"

        Note that there may be multiple style tags in effect at any time (just like nested tags in HTML).
        Styles are applied in a stack, with more recently encountered tags taking precedence. When an
        "end style" string is encountered, the topmost style in the stack is removed.
        
        Arguments:
          tag -- The style tag to be converted into a tag string.
        
        Returns a string representing the tag in a way that is unlikely to occur in normal log output.
        """
        return _style_tag(tag)

    def end_style(self):
        """
        Return an "end style" string.
        
        When the emk logger encounters and "end style" string it will remove the most recently
        applied style (popping it off the stack). 
        """
        return _style_tag('')


def setup(args=[]):
    """Set up emk with the given arguments, and install it into builtins."""
    emk = EMK(args)
    builtins.emk = emk
    return emk

def main(args):
    """
    Execute the emk build process in the current directory.

    Arguments:
      args -- A list of arguments to emk. Arguments can either be options or
              targets.  An option is an argument of the form "key=value". Any
              arguments that do not contain '=' are treated as explicit targets to
              be built. You may specify targets that contain '=' using the special
              option "explicit_target=<target name>". All options (whether or not
              they are recognized by emk) can be accessed via the emk.options dict.
            
              If no explicit targets are specified, emk will build all autobuild
              targets.

    Recognized options:
      log     -- The log level that emk will use. May be one of ["debug", "info",
                 "warning", "error", "critical"], although error and critical are
                 probably not useful. The default value is "info".
      emk_dev -- If set to "yes", developer mode is turned on. Currently this
                 disables stack filtering so that errors within emk can be
                 debugged. The default value is "no".
      threads -- Set the number of threads used by emk for building. May be either
                 a positive number, or "x".  If the value is a number, emk will use
                 that many threads for building; if the value is "x", emk will use
                 as many threads as there are cores on the build machine. The
                 default value is "x".
      style   -- Set the log style mode. May be one of ["no", "console", "html",
                 "passthrough"]. If set to "no", log output styling is disabled. If
                 set to "console", ANSI escape codes will be used to color log
                 output (not yet supported on Windows). If set to "html", the log
                 output will be marked up with <div> and <span> tags that can then
                 be styled using CSS. If set to "passthrough", the style metadata
                 will be output directly (useful if emk is calling itself as a
                 subprocess). The default value is "console".
      trace   -- Specify a set of targets to trace for debugging purposes. The
                 trace for each target will be printed once the build is complete.
                 The targets are specified as a list of comma-separated paths,
                 which may be relative to the current directory or absolute. Build
                 and project directory placeholders will be replaced based on the
                 current directory.
      trace_unchanged -- If set to "yes", the tracer will trace through targets
                         that were not modified as well. The default value is "no".
    """
    emk = None
    try:
        emk = setup(args)
        emk.run(os.getcwd())
        return 0
    except KeyboardInterrupt:
        if emk:
            emk.log.error("\nemk: Interrupted", extra={'adorn':False})
            emk._print_bad_rules()
        return 1
    except _BuildError as e:
        lines = [_style_tag('important') + "Build error:" + _style_tag('') + " %s" % (e)]
        if e.extra_info:
            lines.extend(["    " + line.replace('\n', "\n    ") for line in e.extra_info])
        emk.log.error('\n'.join(lines), extra={'adorn':False})
        emk._print_bad_rules()
        return 1
    finally:
        if emk:
            emk._print_traces()
