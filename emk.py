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
    def __init__(self, local_path, rule):
        self.orig_path = local_path
        self.rule = rule
        if rule:
            self.abs_path = _make_target_abspath(local_path, rule.scope)
        else:
            self.abs_path = local_path
            
        self.attached_deps = set()
        
        self._rebuild_if_changed = False
        
        self._required_by = set()
        self._built = False
        self._visited = False
        
        self._virtual_modtime = None

class _Rule(object):
    def __init__(self, requires, args, func, threadsafe, ex_safe, has_changed_func, scope):
        self.produces = []
        self.requires = requires
        self.args = args
        self.func = func
        self.scope = scope
        self.threadsafe = threadsafe
        self.ex_safe = ex_safe
        
        self._key = None
        self._cache = None
        self._untouched = set()
        
        self._lock = threading.Lock()
        
        self.secondary_deps = set()
        self.weak_deps = set()
        
        self.has_changed_func = has_changed_func
        
        self._required_targets = []
        self._remaining_unbuilt_reqs = 0
        self._want_build = False
        self._built = False
        self.stack = []

class _RuleWrapper(object):
    def __init__(self, func, stack=[], produces=[], requires=[], args=[], threadsafe=False, ex_safe=False, has_changed_func=None):
        self.func = func
        self.stack = stack
        self.produces = produces
        self.requires = requires
        self.args = args
        self.threadsafe = threadsafe
        self.ex_safe = ex_safe
        self.has_changed_func = has_changed_func

    def __call__(self, produces, requires, args):
        return self.func(produces, requires, args)

class _ScopeData(object):
    def __init__(self, parent, scope_type, dir, proj_dir):
        self.parent = parent
        self.scope_type = scope_type
        self.dir = dir
        self.proj_dir = proj_dir
        
        self._cache = None
        self._do_later_funcs = []
        self._wrapped_rules = []
        
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
        self._wrapped_rules = []

class _RuleQueue(object):
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

        self.STOP = object()

    def put(self, rule):
        with self.lock:
            if self.errors:
                return
            self.tasks += 1
            if not rule.threadsafe or not len(self.special_queue) or self.num_threads == 1:
                # not threadsafe, or special queue is empty, so add to special queue
                self.special_queue.append(rule)
                self.special_cond.notify()

            else:
                # add to normal queue
                self.queue.append(rule)
                self.cond.notify()

    def get(self, special):
        with self.lock:
            if self.errors:
                return self.STOP
            if special:
                if len(self.special_queue):
                    return self.special_queue.popleft()
                elif len(self.queue) and not (self.queue[0] is self.STOP):
                    return self.queue.popleft()
                else:
                    while not len(self.special_queue):
                        self.special_cond.wait()
                    return self.special_queue.popleft()
            else:
                while not len(self.queue):
                    self.cond.wait()
                return self.queue.popleft()

    def done_task(self):
        with self.lock:
            if self.errors:
                return
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
    def __init__(self, scope, parent=None):
        if parent:
            self.remove_build_dir = parent.remove_build_dir
        else:
            self.remove_build_dir = True
    
    def new_scope(self, scope):
        return _Clean_Module(scope, self)
    
    def clean_func(self, produces, requires, args):
        build_dir = os.path.realpath(os.path.join(emk.scope_dir, emk.build_dir))
        if self.remove_build_dir:
            if os.path.commonprefix([build_dir, emk.scope_dir]) == emk.scope_dir:
                _clean_log.info("Removing directory %s", build_dir)
                shutil.rmtree(build_dir, ignore_errors=True)
        else:
            _clean_log.info("Not removing directory %s", build_dir)
        emk.mark_virtual(*produces)
    
    def post_rules(self):
        emk.rule(["clean"], [emk.ALWAYS_BUILD], self.clean_func, threadsafe=True, ex_safe=True)

class _Module_Instance(object):
    def __init__(self, name, instance, mod):
        self.name = name
        self.instance = instance
        self.mod = mod

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
        
class _ConsoleStyler(object):
    def __init__(self):
        self.r = re.compile("\000\001([0-9A-Za-z_]*)\001\000")
        self.styles = {"bold":"\033[1m", "u":"\033[4m", "red":"\033[31m", "green":"\033[32m", "blue":"\033[34m",
            "important":"\033[1m\033[31m", "rule_stack":"\033[34m", "stderr":"\033[31m"}
        
    def style(self, string, record):
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
        m = self.r.search(string, start)
        while m:
            bits.append(string[start:m.start(0)])
            style = styles.get(m.group(1))
            if style is not None:
                stack.append(style)
                bits.append(style)
            elif m.group(1) == '':
                prevstyle = stack.pop()
                if prevstyle:
                    bits.append("\033[0m")
                    bits.append(''.join(stack))
            else:
                stack.append('')
            
            start = m.end(0)
            m = self.r.search(string, start)
        bits.append(string[start:])
        bits.append("\033[0m")
        return ''.join(bits)
        
class _HtmlStyler(object):
    def __init__(self):
        self.r = re.compile("\000\001([0-9A-Za-z_]+)\001\000")
        
    def style(self, string, record):
        string = string.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        string = self.r.sub(r'<span class="emk_\1">', string)
        string = string.replace("\000\001\001\000", '</span>').replace("\n", "<br>").replace("    ", "&nbsp;&nbsp;&nbsp;&nbsp;")
        return '<p class="emk_log emk_%s">' % (record.orig_levelname) + string + '</p>'

class _Formatter(logging.Formatter):
    def __init__(self, format):
        self.format_str = format
        self.styler = _NoStyler()

    def format(self, record):
        record.message = record.getMessage()
        record.orig_levelname = record.levelname.lower()
        record.levelname = _style_tag('logtype') + record.levelname.lower() + _style_tag('')
            
        if record.__dict__.get("adorn") is False:
            return self.styler.style(record.message, record)
        
        return self.styler.style(self.format_str % record.__dict__, record)

def _find_project_dir(path):
    dir = path
    prev = None
    while dir != prev:
        if os.path.isfile(os.path.join(dir, "emk_project.py")):
            return dir
            
        prev = dir
        dir, tail = os.path.split(dir)
    return dir

def _mkdirs(path):
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise

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
    if os.path.isabs(rel_path):
        return rel_path
    return os.path.join(scope.dir, rel_path)

def _make_target_abspath(rel_path, scope):
    if rel_path.startswith(emk.proj_dir_placeholder):
        rel_path = rel_path.replace(emk.proj_dir_placeholder, scope.proj_dir, 1)
    path = rel_path.replace(emk.build_dir_placeholder, scope.build_dir)
    return os.path.realpath(_make_abspath(path, scope))

def _make_require_abspath(rel_path, scope):
    if rel_path is emk.ALWAYS_BUILD:
        return emk.ALWAYS_BUILD

    if rel_path.startswith(emk.proj_dir_placeholder):
        rel_path = rel_path.replace(emk.proj_dir_placeholder, scope.proj_dir, 1)
    return os.path.realpath(_make_abspath(rel_path, scope))

class EMK_Base(object):
    def __init__(self, args):
        global emk_dev
        
        self._flatten_gen = _flatten_gen
        
        self.log = logging.getLogger("emk")
        handler = logging.StreamHandler(sys.stdout)
        formatter = _Formatter("%(name)s (%(levelname)s): %(message)s")
        handler.setFormatter(formatter)
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
        
        stylers = {"no":_NoStyler, "console":_ConsoleStyler, "html":_HtmlStyler}
        
        if sys.platform == "win32":
            self._options["colors"] = "no"
        else:
            self._options["colors"] = "console"
        
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
                    elif key == "colors":
                        if val not in stylers:
                            self.log.error("Unknown color style option '%s'", level)
                            val = "no"
                            
                    self._options[key] = val
            else:
                self._explicit_targets.add(arg)
        
        formatter.styler = stylers[self._options["colors"]]()
        
        self.log.info("Using %d %s", self._build_threads, ("thread" if self._build_threads == 1 else "threads"))
        
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
                    n = begin + self._known_build_dirs[d] + end
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
                target.attached_deps = target.attached_deps.union(new_deps)
                if target._built: # need to build now, since it was attached to something that was already built
                    l = [self._get_target(a) for a in new_deps]
                    dep_targets = [t for t in l if t]
                    for d in dep_targets:
                        if not d._built:
                            self._must_build.append(d)
            else:
                self.log.info("Target %s was attached to, but not yet defined as a product of a rule", path)

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
        for path in self._rebuild_if_changed:
            t = self._targets.get(path)
            if t:
                t._rebuild_if_changed = True
        
    def _setup_rule_cache(self, rule):
        paths = [t.abs_path for t in rule.produces]
        paths.sort()
        rule._key = key = hashlib.md5('\0'.join(paths)).hexdigest()
        cache = rule.scope._cache.get(key)
        if cache is None:
            rule.scope._cache[key] = cache = {}
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

        for path in target.attached_deps:
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
            cache = rule._cache[abs_path]
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
                            del rule.scope._cache[rule._key]
                        raise _BuildError("%s should have been produced by the rule" % (abs_path), rulestack)
            else:
                changed = False
            
            if virtual and not changed:
                modtime = cache.get("vmodtime")
                if modtime is None:
                    cache["vmodtime"] = modtime = 0
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
    
    def _req_has_changed(self, rule, req, cache, weak):
        if req.abs_path is self.ALWAYS_BUILD:
            return True
            
        virtual_modtime = req._virtual_modtime
        if virtual_modtime is None:
            return rule.has_changed_func(req.abs_path, cache, weak)
        else:
            cached_modtime = cache.get("vmodtime")
            if cached_modtime != virtual_modtime:
                self.log.debug("Modtime (virtual) for %s has changed; cached = %s, actual = %s", req.abs_path, cached_modtime, virtual_modtime)
                cache["vmodtime"] = virtual_modtime
                if weak and cached_modtime is None:
                    return False
                return True
            else:
                self.log.debug("Modtime (virtual) for %s has not changed (%s)", req.abs_path, cached_modtime)
            return False
    
    def has_changed_func(self, abs_path, cache, weak=False):
        try:
            modtime = os.path.getmtime(abs_path)
            cached_modtime = cache.get("modtime")
            if cached_modtime != modtime:
                self.log.debug("Modtime for %s has changed; cached = %s, actual = %s", abs_path, cached_modtime, modtime)
                cache["modtime"] = modtime
                if weak and cached_modtime is None:
                    return False
                return True
            else:
                self.log.debug("Modtime for %s has not changed (%s)", abs_path, cached_modtime)
            return False
        except OSError:
            raise _BuildError("Could not get modtime for %s" % (abs_path))
    
    def _get_changed_reqs(self, rule):
        changed_reqs = []
        for req, weak in rule._required_targets:
            if req.abs_path is self.ALWAYS_BUILD:
                changed_reqs.append(req.abs_path)
            else:
                rcache = rule._cache[req.abs_path]
                if self._req_has_changed(rule, req, rcache, weak):
                    changed_reqs.append(req.abs_path)
        return changed_reqs
    
    def _fixup_rule_cache(self, rule):
        path_set = (set([t.abs_path for t in rule.produces]) | set([r.abs_path for r, w in rule._required_targets])) - set([self.ALWAYS_BUILD])
        cache = rule._cache
        for p in path_set:
            if p not in cache:
                cache[p] = {}
    
    def _build_thread_func(self, special):
        self._local.current_rule = None
        while(True):
            rule = self._buildable_rules.get(special)
            if rule is self._buildable_rules.STOP:
                return
            
            self._fixup_rule_cache(rule)
            
            try:
                rule._built = True

                need_build = False
                changed_reqs = self._get_changed_reqs(rule)
                if changed_reqs:
                    need_build = True
                    self.log.debug("Need to build %s because dependencies %s have changed", [t.abs_path for t in rule.produces], changed_reqs)
                else:
                    for t in rule.produces:
                        tcache = rule._cache[t.abs_path]
                        if not tcache.get("virtual", False): # virtual products of this rule cannot be modified externally
                            if not os.path.exists(t.abs_path):
                                self.log.debug("Need to build %s because it does not exist", t.abs_path)
                                need_build = True
                            elif t._rebuild_if_changed and rule.has_changed_func(t.abs_path, tcache):
                                self.log.debug("Need to build %s because it has changed", t.abs_path)
                                need_build = True

                if need_build:
                    self._local.current_scope = rule.scope
                    if not rule.threadsafe:
                        os.chdir(rule.scope.dir)
                    produces = [p.abs_path for p in rule.produces]
            
                    self.scope.prepare_do_later()
                    self._local.current_rule = rule
                    rule.func(produces, rule.requires, rule.args)
                    self._run_do_later_funcs()
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
            
            self._buildable_rules.done_task()

    def _do_build(self):
        # if there are explicit targets, see if we have rules for all of them
        # if so, just build those targets (and we should stop the build process)
        # otherwise, build all explicit targets and autobuild targets that we can

        self._building = True
        self._load_scope_caches()

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
        
        for wrapper in self.scope._wrapped_rules:
            self._rule(wrapper.produces, wrapper.requires, wrapper.args, wrapper.func, wrapper.threadsafe, wrapper.ex_safe, wrapper.has_changed_func, wrapper.stack)
    
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
        proj_dir = None
        new_subproj_dirs = []
        parent_scope = self._root_scope
        d = path
        prev = None
        while d != prev:
            if d in self._stored_subproj_scopes:
                parent_scope = self._stored_subproj_scopes[d]
                break
            if os.path.isfile(os.path.join(d, "emk_subproj.py")):
                new_subproj_dirs.append((d, True))
            else:
                new_subproj_dirs.append((d, False))
            if os.path.isfile(os.path.join(d, "emk_project.py")):
                proj_dir = d
                break
            prev = d
            d, tail = os.path.split(d)
        
        if proj_dir:
            self._local.current_scope = _ScopeData(self._root_scope, "project", proj_dir, proj_dir)
            
            self._local.current_scope.prepare_do_later()
            self.import_from([proj_dir], "emk_project")
            self._run_module_post_functions()
            self._run_do_later_funcs()
            
            parent_scope = self._local.current_scope
        elif parent_scope is self._root_scope:
            proj_dir = d
        else:
            proj_dir = parent_scope.proj_dir
        
        new_subproj_dirs.reverse()
        for d, have_subproj in new_subproj_dirs:
            if have_subproj:
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
    
    def _load_scope_caches(self):
        start_time = time.time()
        for path, scope in self._visited_dirs.items():
            if scope._cache is None:
                scope._cache = {}
                if not self.cleaning:
                    cache_path = os.path.join(path, scope.build_dir, "__emk_cache__")
                    try:
                        with open(cache_path, "rb") as f:
                            scope._cache = pickle.load(f)
                    except IOError:
                        pass
        self._load_cache_time += (time.time() - start_time)
    
    def _write_scope_caches(self):
        if self.cleaning:
            return
        start_time = time.time()
        for path, scope in self._visited_dirs.items():
            cache_path = os.path.join(path, scope.build_dir, "__emk_cache__")
            if scope._cache:
                try:
                    with open(cache_path, "wb") as f:
                        pickle.dump(scope._cache, f, -1)
                except IOError:
                    self.log.error("Failed to open cache file %s", cache_path)
                except:
                    try:
                        os.remove(cache_path)
                    except OSError:
                        pass
                    raise
            else:
                try:
                    os.remove(cache_path)
                except OSError:
                    pass
        self._load_cache_time += (time.time() - start_time)
    
    def _handle_dir(self, d, first_dir=False):
        path = os.path.realpath(d)
        if path in self._visited_dirs:
            return
        
        try:
            os.chdir(path)
        except OSError:
            self.log.warning("Failed to change to directory %s", path)
            return
            
        self.log.info("Entering directory %s", path)
        
        self._load_parent_scope(path)
        self._local.current_scope = _ScopeData(self._local.current_scope, "rules", path, self._current_proj_dir)
        
        self.scope.prepare_do_later()
        self.module(self.scope.pre_modules) # load preload modules
        if not self.import_from([path], "emk_rules"):
            self.module(self.scope.default_modules)
        self._run_do_later_funcs()
        
        self.scope.prepare_do_later()
        self._run_module_post_functions()
        self._run_do_later_funcs()
        
        self._known_build_dirs[path] = self.scope.build_dir
        
        # gather dirs to (potentially) recurse into
        recurse_dirs = self.scope.recurse_dirs
        self.scope.recurse_dirs = set()
        
        if not self._cleaning:
            _mkdirs(self.scope.build_dir)
        if first_dir:
            self._fix_explicit_targets()
        
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
    
    def _rule(self, produces, requires, args, func, threadsafe, ex_safe, has_changed_func, stack):
        if self.scope_name != "rules":
            self.log.warning("Cannot create rules when not in 'rules' scope (current scope = '%s')", self.scope_name)
            return
        seen_produces = set([emk.ALWAYS_BUILD])
        fixed_produces = []
        for p in _flatten_gen(produces):
            if p and p not in seen_produces:
                seen_produces.add(p)
                fixed_produces.append(p)
        fixed_requires = [_make_require_abspath(r, self.scope) for r in _flatten_gen(requires) if r != ""]
        
        if not has_changed_func:
            has_changed_func = self.has_changed_func

        new_rule = _Rule(fixed_requires, args, func, threadsafe, ex_safe, has_changed_func, self.scope)
        new_rule.stack = stack
        with self._lock:
            self._rules.append(new_rule)
            for product in fixed_produces:
                new_target = _Target(product, new_rule)
                if new_target.abs_path in self._targets and self._targets[new_target.abs_path].rule:
                    self.log.warning("Duplicate rule producing %s", new_target.abs_path)
                else:
                    if new_target.abs_path in self._aliases:
                        self.log.warning("Alias %s is produced by a rule; removing" % (new_target.abs_path))
                        del self._aliases[new_target.abs_path]

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

class EMK(EMK_Base):
    def __init__(self, args):
        super(EMK, self).__init__(args)
        
        self.BuildError = _BuildError
        self.Target = _Target
        self.Rule = _Rule
        self.Container = _Container

    def _set_build_dir(self, dir):
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
        if self._did_run:
            stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
            raise _BuildError("Cannot call run() again", stack)
        self._did_run = True
        
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
        self._load_cache_time = 0
        
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
            
                # fix up requires (set up absolute paths, and map to targets)
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
                
                now = time.time()
                self._time_lines.append("Build phase %d: %0.3f seconds" % (self._build_phase, now - phase_start_time))
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
        
        if not self.cleaning:
            self._time_lines.append("Load/store caches: %0.3f seconds" % (self._load_cache_time))
        
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
            self.log.info(line)
        diff = time.time() - start_time
        self.log.info("Finished in %0.3f seconds" % (diff))
    
    def import_from(self, paths, name):
        return self._import_from(paths, name)
    
    def insert_module(self, name, instance):
        if self.building:
            stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
            raise _BuildError("Cannot call insert_module() when building", stack)
            
        if name in self.scope.modules or name in self.scope.weak_modules:
            self.log.warning("Cannot insert over pre-existing '%s' module", name)
            return None
        
        mod = _Module_Instance(name, instance, None)
        _try_call_method(mod, "load_" + self.scope.scope_type)
        self.scope.weak_modules[name] = mod
        return instance
    
    def module(self, *names):
        mods = []
        for name in _flatten_gen(names):
            mods.append(self._module(name, weak=False))
        if len(mods) == 1:
            return mods[0]
        return mods

    def weak_module(self, *names):
        mods = []
        for name in _flatten_gen(names):
            mods.append(self._module(name, weak=True))
        if len(mods) == 1:
            return mods[0]
        return mods
    
    # 0-length produces and requires ("") are ignored. A require of emk.ALWAYS_BUILD means that this rule must always be built
    def rule(self, produces, requires, func, args=[], threadsafe=False, ex_safe=False, has_changed_func=None):
        stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
        self._rule(produces, requires, args, func, threadsafe, ex_safe, has_changed_func, stack)
    
    # decorator for simple rule creation
    def produces(self, *targets):
        def decorate(f):
            if isinstance(f, _RuleWrapper):
                f.produces = targets
                return f
            else:
                stack = _format_decorator_stack(_filter_stack(traceback.extract_stack()[:-1]))
                wrapper = _RuleWrapper(f, stack, produces=targets)
                self.scope._wrapped_rules.append(wrapper)
                return wrapper
        return decorate

    # decorator for simple rule creation
    def requires(self, *depends):
        def decorate(f):
            if isinstance(f, _RuleWrapper):
                f.requires = depends
                return f
            else:
                stack = _format_decorator_stack(_filter_stack(traceback.extract_stack()[:-1]))
                wrapper = _RuleWrapper(f, stack, requires=depends)
                self.scope._wrapped_rules.append(wrapper)
                return wrapper
        return decorate
    
    def depend(self, target, *dependencies):
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
        fixed_depends = [_make_require_abspath(d, self.scope) for d in _flatten_gen(attached_targets) if d != ""]
        abspath = _make_target_abspath(target, self.scope)
        self.log.debug("Attaching %s to target %s", fixed_depends, abspath)
        with self._lock:
            if abspath in self._attached_dependencies:
                self._attached_dependencies[abspath].extend(fixed_depends)
            else:
                self._attached_dependencies[abspath] = list(fixed_depends)
    
    def autobuild(self, *targets):
        with self._lock:
            for target in _flatten_gen(targets):
                self.log.debug("Marking %s for automatic build", target)
                self._auto_targets.add(_make_target_abspath(target, self.scope))
    
    def alias(self, target, alias):
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
        with self._lock:
            for path in _flatten_gen(paths):
                abs_path = _make_require_abspath(path, self.scope)
                self.log.debug("Requiring %s to be built by an explicit rule", abs_path)
                self._requires_rule.add(abs_path)
    
    def rebuild_if_changed(self, *paths):
        with self._lock:
            for path in _flatten_gen(paths):
                abs_path = _make_target_abspath(path, self.scope)
                self.log.debug("Requiring %s to be rebuilt if it has changed", abs_path)
                self._rebuild_if_changed.add(abs_path)
    
    def recurse(self, *paths):
        for path in _flatten_gen(paths):
            abspath = _make_target_abspath(path, self.scope)
            self.log.debug("Adding recurse directory %s", abspath)
            self.scope.recurse_dirs.add(abspath)
    
    def subdir(self, *paths):
        self.recurse(paths)
        sub_cleans = [os.path.join(path, "clean") for path in _flatten_gen(paths)]
        self.attach("clean", *sub_cleans)
    
    def do_later(self, func):
        self.scope._do_later_funcs.append(func)
    
    def do_prebuild(self, func):
        with self._lock:
            self._prebuild_funcs.append((self.scope, func))
    
    def do_postbuild(self, func):
        with self._lock:
            self._postbuild_funcs.append((self.scope, func))
    
    def mark_virtual(self, *paths):
        rule = self.current_rule
        if not rule:
            self.log.warning("Cannot mark anything as virtual when not in a rule")
            return
        
        abs_paths = set([_make_target_abspath(path, self.scope) for path in _flatten_gen(paths)])
        cache = rule._cache
        for path in abs_paths:
            tcache = cache.get(path)
            # If there is no entry in the rule cache for the path, it is not a rule product
            # Note that there may be other entries in the rule cache that are not products,
            # but marking them as virtual has no effect (so it is harmless).
            if tcache is None:
                self.log.debug("Cannot mark %s as virtual since it is not a rule product", path)
            else:
                self.log.debug("Marking %s as virtual", path)
                tcache["virtual"] = True
    
    def mark_untouched(self, *paths):
        if not self.current_rule:
            self.log.warning("Cannot mark anything as untouched when not in a rule")
            return
            
        untouched_set = self.current_rule._untouched
        for path in _flatten_gen(paths):
            abs_path = _make_target_abspath(path, self.scope)
            self.log.debug("Marking %s as untouched", abs_path)
            untouched_set.add(abs_path)
    
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
            cache = rule._cache.get(key)
            if cache is None:
                rule._cache[key] = cache = {}
            return cache
        return None
    
    def abspath(self, path):
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
        return _style_tag(tag)

    def end_style(self):
        return _style_tag('')


def setup(args=[]):
    """Set up EMK with the given arguments, and install it into builtins."""
    emk = EMK(args)
    builtins.emk = emk
    return emk

def main(args):
    """
    Execute the emk build process in the current directory.
    
    Arguments:
    args -- A list of arguments to EMK. Arguments can either be options or targets.
            An option is an argument of the form "key=value". Any arguments that do not contain '=' are treated
            as explicit targets to be built. You may specify targets that contain '=' using the special option
            "explicit_target=<target name>". All options (whether or not they are recognized by EMK) can be
            accessed via the emk.options dict.
            
            If no explicit targets are specified, EMK will build all autobuild targets.
    
    Recognized options:
    log     -- The log level that emk will use. May be one of ["debug", "info", "warning", "error", "critical"],
               although error and critical are probably not useful. The default value is "info".
    emk_dev -- If set to "yes", developer mode is turned on. Currently this disables stack filtering so
               that errors within EMK can be debugged. The default value is "no".
    threads -- Set the number of threads used by EMK for building. May be either a positive number, or "x".
               If the value is a number, EMK will use that many threads for building; if the value is "x",
               EMK will use as many threads as there are cores on the build machine. The default value is "x".
    colors  -- Set the log coloring mode. May be one of ["no", "console", "html"]. If set to "no", log output coloring
               is disabled. If set to "console", ANSI escape codes will be used to color log output (not yet supported
               on Windows). If set to "html", the log output will be marked up with <p> and <span> tags that can then
               be styled using CSS. The default value is "console".
    """
    try:
        setup(args).run(os.getcwd())
        return 0
    except KeyboardInterrupt:
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
