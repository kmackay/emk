#!/usr/bin/python

from __future__ import print_function

import os
import sys
import imp
if sys.version_info[0] < 3:
    import __builtin__ as builtins
else:
    import builtins
import logging
import collections
import errno
import traceback
import time
import json
import threading
import shutil
import multiprocessing

_module_path = os.path.realpath(os.path.abspath(__file__))

class _Target(object):
    def __init__(self, local_path, rule):
        self.orig_path = local_path
        self.rule = rule
        if rule:
            self.abs_path = _make_target_abspath(local_path, rule.scope)
        else:
            self.abs_path = local_path
            
        self.attached_deps = set()
        
        self.mod_time = None
        
        self._required_by = set()
        self._built = False
        self._visited = False
        self._untouched = False

class _Rule(object):
    def __init__(self, requires, args, func, threadsafe, scope):
        self.produces = []
        self.requires = requires
        self.args = args
        self.func = func
        self.scope = scope
        self.threadsafe = threadsafe
        
        self.secondary_deps = set()
        
        self._required_targets = []
        self._remaining_unbuilt_reqs = 0
        self._want_build = False
        self._built = False
        self.stack = []

class _RuleWrapper(object):
    def __init__(self, func, stack=[], produces=[], requires=[], args=[], threadsafe=False):
        self.func = func
        self.stack = stack
        self.produces = produces
        self.requires = requires
        self.args = args
        self.threadsafe = threadsafe

    def __call__(self, produces, requires, args):
        return self.func(produces, requires, args)

class _ScopeData(object):
    def __init__(self, parent, scope_type, dir, proj_dir):
        self.parent = parent
        self.scope_type = scope_type
        self.dir = dir
        self.proj_dir = proj_dir
        
        self.modtime_cache = {}
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
        build_dir = os.path.realpath(os.path.join(emk.current_dir, emk.build_dir))
        if self.remove_build_dir:
            if os.path.commonprefix([build_dir, emk.current_dir]) == emk.current_dir:
                _clean_log.info("Removing directory %s", build_dir)
                shutil.rmtree(build_dir, ignore_errors=True)
        else:
            _clean_log.info("Not removing directory %s", build_dir)
        emk.mark_exists(*produces)
    
    def post_rules(self):
        emk.rule(["clean"], [emk.ALWAYS_BUILD], self.clean_func, threadsafe=True)

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

class _Formatter(logging.Formatter):
    def __init__(self, format):
        self.format_str = format

    def format(self, record):
        record.message = record.getMessage()
        if "adorn" in record.__dict__ and not record.__dict__["adorn"]:
            return record.message
        record.levelname = record.levelname.lower()
        return self.format_str % record.__dict__

def _find_project_dir():
    dir = os.getcwd()
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

def _get_exception_info():
    t, value, trace = sys.exc_info()
    stack = traceback.extract_tb(trace)
    lines = ["%s: %s" % (t.__name__, str(value))]
    lines.extend(_format_stack(_filter_stack(stack)))
    return lines

def _filter_stack(stack):
    new_stack = []
    entered_emk = False
    left_emk = False
    for item in stack:
        if left_emk:
            new_stack.append(item)
        elif entered_emk:
            if item[0] != __file__:
                left_emk = True
                new_stack.append(item)
        elif item[0] == __file__:
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
        self.log = logging.getLogger("emk")
        handler = logging.StreamHandler()
        formatter = _Formatter("%(name)s (%(levelname)s): %(message)s")
        handler.setFormatter(formatter)
        self.log.addHandler(handler)
        self.log.propagate = False
        self.log.setLevel(logging.INFO)
        
        self.build_dir_placeholder = "$:build:$"
        self.proj_dir_placeholder = "$:proj:$"
        
        global _module_path
        self._emk_dir, tail = os.path.split(_module_path)
        
        self._local = threading.local()
        
        self._prebuild_funcs = [] # list of (scope, func)
        self._postbuild_funcs = [] # list of (scope, func)
        
        self._targets = {} # map absolute target name -> target
        self._rules = []
        
        self._visited_dirs = {}
        self._current_proj_dir = None
        self._stored_proj_scopes = {} # map project path to loaded project scope (_ScopeData)
        self._known_build_dirs = {} # map path to build dir defined for that path
        
        self._all_loaded_modules = {} # map absolute module path to module
        
        self.ALWAYS_BUILD = _Always_Build()
        
        self._auto_targets = set()
        self._aliases = {}
        self._attached_dependencies = {}
        self._secondary_dependencies = {}
        self._allowed_nonexistent = set()
        self._requires_rule = set()
        
        self._fixed_aliases = {}
        self._fixed_auto_targets = []
        self._must_build = []
        
        self._modtime_cache = {}
        
        self._done_build = False
        self._added_rule = False
        
        self._cleaning = False
        self._building = False
        self._did_run = False
        
        self._toplevel_examined_targets = set()
        
        self._lock = threading.Lock()
        self._build_threads = 1
        
        # parse args
        log_levels = {"debug":logging.DEBUG, "info":logging.INFO, "warning":logging.WARNING, "error":logging.ERROR, "critical":logging.CRITICAL}
        
        self._options = {}
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
                    elif key == "threads":
                        if val == "x":
                            val = multiprocessing.cpu_count()
                        else:
                            try:
                                val = int(val, base=0)
                                if val < 1:
                                    val = 1
                            except ValueError:
                                self.log.error("Thread count '%s' cannot be converted to an integer", val)
                                val = 1
                        self._build_threads = val
                        self.log.info("Using %d threads", val)
                    self._options[key] = val
            else:
                self._explicit_targets.add(arg)
        
        if "clean" in self._explicit_targets:
            self._cleaning = True
            self._explicit_targets = set(["clean"])
    
    scope = property(lambda self: self._local.current_scope)
    
    def _push_scope(self, scope_type, dir):
        self._local.current_scope = _ScopeData(self._local.current_scope, scope_type, dir, self._current_proj_dir)

    def _pop_scope(self):
        self._local.current_scope = self._local.current_scope.parent
    
    def _import_from(self, paths, name, set_scope_dir=False):
        if self.building:
            stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
            raise _BuildError("Cannot call import_from() when building", stack)

        fixed_paths = [_make_target_abspath(path, self.scope) for path in paths]

        oldpath = os.getcwd()
        fp = None
        try:
            fp, pathname, description = imp.find_module(name, fixed_paths)
            mpath = os.path.realpath(os.path.abspath(pathname))
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
        if path in self._targets:
            return self._targets[path]
        elif path in self._fixed_aliases:
            return self._fixed_aliases[path]
        elif create_new:
            self.log.debug("Creating artificial target for %s", path)
            target = _Target(path, None)
            self._targets[path] = target
            return target

    def _resolve_build_dirs(self, dirs, ignore_errors=False):
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
                elif not ignore_errors:
                    raise _BuildError("Could not resolve %s for path %s" % (self.build_dir_placeholder, path))
                else:
                    updated_paths.append(None)
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
                if target in self._targets:
                    did_fix = True
                    fixed_aliases[alias] = self._targets[target]
                    self.log.debug("fixed alias %s => %s", alias, self._targets[target].abs_path)
                elif target in fixed_aliases:
                    did_fix = True
                    fixed_aliases[alias] = fixed_aliases[target]
                    self.log.debug("fixed alias %s => %s", alias, fixed_aliases[target].abs_path)
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
                self.log.warning("Target %s was attached to, but not defined as a product of a rule", path)

    def _fix_requires(self, rule):
        if rule._built:
            return

        rule.requires = self._resolve_build_dirs(rule.requires)
        
        secondaries = set(self._resolve_build_dirs(rule.secondary_deps))
        updated_paths = secondaries.union(set(rule.requires))

        # convert paths to actual targets
        required_targets = []   
        for path in updated_paths:
            target = self._get_target(path, create_new=True)

            target._required_by.add(rule) # when the target is built, we need to know which rules to examine for further building
            required_targets.append(target)

        rule._required_targets = required_targets

    def _fix_auto_targets(self):
        self._fixed_auto_targets = []
        for path in self._auto_targets:
            self._fixed_auto_targets.append(self._get_target(path, create_new=True))
        self._auto_targets.clear() # don't need these anymore since they will be built from the fixed list if necessary
    
    def _fix_allowed_nonexistent(self):
        self._allowed_nonexistent = set(self._resolve_build_dirs(self._allowed_nonexistent, ignore_errors=True))
    
    def _fix_requires_rule(self):
        self._requires_rule = set(self._resolve_build_dirs(self._requires_rule))
    
    def _toplevel_examine_target(self, target):
        if not target in self._toplevel_examined_targets:
            self._toplevel_examined_targets.add(target)
            self._examine_target(target)

    def _examine_target(self, target):
        if target._visited or target._built:
            return
        target._visited = True
        self.log.debug("Examining target %s", target.abs_path)

        for path in target.attached_deps:
            t = self._get_target(path)
            if t:
                self._examine_target(t)

        if not target.rule:
            if not target.abs_path in self._requires_rule:
                exists, target.mod_time = self._get_mod_time(target.abs_path)
                if exists:
                    target._built = True
                elif target.abs_path in self._allowed_nonexistent:
                    self.log.debug("Allowing nonexistent %s", target.abs_path)
                    target.mod_time = time.time()
                    target._built = True
        elif not target.rule._want_build:
            target.rule._want_build = True
            target.rule._remaining_unbuilt_reqs = 0
            for req in target.rule._required_targets:
                self._examine_target(req)
                if not req._built: # we can't build this rule immediately
                    target.rule._remaining_unbuilt_reqs += 1

            if not target.rule._remaining_unbuilt_reqs:
                # can build this rule immediately
                self._add_buildable_rule(target.rule)
    
    def _add_buildable_rule(self, rule):
        self._buildable_rules.put(rule)

    def _done_rule(self, rule, built):
        with self._lock:
            now = time.time()
            for t in rule.produces:
                abs_path = t.abs_path
                exists, m = self._get_mod_time(abs_path, lock=False)
                
                if not exists:
                    raise _BuildError("%s should have been produced by the rule" % (abs_path), rule.stack)
                
                if abs_path in self._modtime_cache:
                    if not t._untouched and built:
                        self._modtime_cache[abs_path][1] = now
                else:
                    if t._untouched:
                        cache = [False, 0, {}]
                    elif built:
                        cache = [False, now, {}]
                    else:
                        cache = [False, m, {}]
                    self._modtime_cache[abs_path] = cache
                    rule.scope.modtime_cache[abs_path] = cache
                
                t.mod_time = self._modtime_cache[abs_path][1]
                self.log.debug("Set modtime for %s to %s", t.abs_path, t.mod_time)
                t._built = True

            for t in rule.produces:
                for r in t._required_by:
                    if not r._want_build:
                        continue
                    
                    r._remaining_unbuilt_reqs -= 1
                    if r._remaining_unbuilt_reqs == 0:
                        self._add_buildable_rule(r)
    
    def _build_thread_func(self, special):
        while(True):
            rule = self._buildable_rules.get(special)
            if rule is self._buildable_rules.STOP:
                return
            
            try:
                rule._built = True

                newest_req_path = ""
                newest_req = 0
                for req in rule._required_targets:
                    if req.mod_time > newest_req:
                        newest_req_path = req.abs_path
                        newest_req = req.mod_time

                need_build = False
                for t in rule.produces:
                    exists, t.mod_time = self._get_mod_time(t.abs_path)
                    if not exists:
                        self.log.debug("Need to build %s because it does not exist", t.abs_path)
                        need_build = True
                    elif t.mod_time < newest_req:
                        self.log.debug("Need to build %s because dependency %s is newer (%s > %s)", t.abs_path, newest_req_path, newest_req, t.mod_time)
                        need_build = True

                if need_build:
                    self._local.current_scope = rule.scope
                    if not rule.threadsafe:
                        os.chdir(rule.scope.dir)
                    produces = [p.abs_path for p in rule.produces]
            
                    self.scope.prepare_do_later()
                    rule.func(produces, rule.requires, rule.args)
                    self._run_do_later_funcs()

                self._done_rule(rule, need_build)
            except _BuildError as e:
                self._buildable_rules.error(e)
                return
            except Exception as e:
                lines = ["    %s" % (line) for line in _get_exception_info()]
                lines.append("Rule definition:")
                lines.extend(["    %s" % (line) for line in rule.stack])
                self._buildable_rules.error(_BuildError("Error running rule", lines))
                return
            
            self._buildable_rules.done_task()

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
        
        # revisit all targets that we want to build that were not built previously
        for target in self._toplevel_examined_targets:
            if not target._built:
                self._examine_target(target)

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

            if (not self._explicit_targets) or leftover_explicit_targets:
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
        
        self._buildable_rules.join()
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
            self._rule(wrapper.produces, wrapper.requires, wrapper.args, wrapper.func, wrapper.threadsafe, wrapper.stack)
    
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

                    self._pop_scope()

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
        env_paths = os.environ.get('EMK_CONFIG_DIRS')
        if env_paths:
            search_paths = env_paths.split(':')
        
        self._import_from(search_paths, "emk_global", set_scope_dir=True)
        self._run_module_post_functions()
        self._run_do_later_funcs()
    
    def _load_proj_scope(self, path):
        if path != self._current_proj_dir:
            self._current_proj_dir = path
            
            if path in self._stored_proj_scopes:
                self._local.current_scope = self._stored_proj_scopes[path]
            else:
                self._pop_scope()
                
                self._push_scope("project", path)
                
                self.scope.prepare_do_later()
                self.import_from([path], "emk_project")
                self._run_module_post_functions()
                self._run_do_later_funcs()
                
                self._stored_proj_scopes[path] = self.scope
    
    def _get_mod_time(self, filepath, lock=True):
        if filepath is self.ALWAYS_BUILD:
            return (True, float("inf"))

        cache = None
        if lock:
            with self._lock:
                if filepath in self._modtime_cache:
                    cache = self._modtime_cache[filepath][0:2]
        elif filepath in self._modtime_cache:
            cache = self._modtime_cache[filepath][0:2]

        if cache and cache[0]:
            return cache
        try:
            modtime = os.path.getmtime(filepath)
            if cache:
                return (True, cache[1])
            return (True, modtime)
        except Exception:
            return (False, 0)
    
    def _load_modtime_cache(self):
        self.scope.modtime_cache = {}
        try:
            with open(os.path.join(self.scope.build_dir, "__emk_cache__")) as f:
                cache = json.load(f)
                # handle duplicate entries by always using the most recent value
                for entry, value in cache.items():
                    if entry in self._modtime_cache:
                        existing_value = self._modtime_cache[entry]
                        if value[1] > existing_value[1]:
                            self._modtime_cache[entry] = value
                        else:
                            value = existing_value
                    else:
                        self._modtime_cache[entry] = value
                    self.scope.modtime_cache[entry] = value
        except IOError:
            pass
    
    def _write_modtime_caches(self):
        if self.cleaning:
            return
        for path, scope in self._visited_dirs.items():
            if scope.modtime_cache:
                cache_path = os.path.join(path, scope.build_dir, "__emk_cache__")
                try:
                    with open(cache_path, "w") as f:
                        json.dump(scope.modtime_cache,f)
                except IOError:
                    self.log.error("Failed to open cache file %s", cache_path)
    
    def _handle_dir(self, dir, first_dir=False):
        path = os.path.realpath(os.path.abspath(dir))
        if path in self._visited_dirs:
            return
        
        try:
            os.chdir(path)
        except OSError:
            self.log.warning("Failed to change to directory %s", path)
            return
            
        self.log.info("Entering directory %s", path)
        
        self._load_proj_scope(_find_project_dir())
        
        self._push_scope("rules", path)
        
        self.scope.prepare_do_later()
        self.modules(*self.scope.pre_modules) # load preload modules
        if not self.import_from([path], "emk_rules"):
            self.modules(*self.scope.default_modules)
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
        self._load_modtime_cache()
        if first_dir:
            self._fix_explicit_targets()
        
        self._visited_dirs[path] = self.scope
        
        self._pop_scope()
        
        for d in recurse_dirs:
            self._handle_dir(d)
    
    def _rule(self, produces, requires, args, func, threadsafe, stack):
        fixed_produces = set([p for p in produces if p != ""])
        fixed_requires = [_make_require_abspath(r, self.scope) for r in requires if r != ""]

        new_rule = _Rule(fixed_requires, args, func, threadsafe, self.scope)
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
    current_dir = property(lambda self: self.scope.dir)
    build_dir = property(lambda self: self.scope.build_dir, _set_build_dir)
    module_paths = property(lambda self: self.scope.module_paths, _set_module_paths)
    default_modules = property(lambda self: self.scope.default_modules, _set_default_modules)
    pre_modules = property(lambda self: self.scope.pre_modules, _set_pre_modules)
    
    local_targets = property(lambda self: self.scope.targets)
    
    def run(self, path):
        if self._did_run:
            stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
            raise _BuildError("Cannot call run() again", stack)
        self._did_run = True
        
        root_scope = _ScopeData(None, "global", os.path.realpath(os.path.abspath(path)), _find_project_dir())
        root_scope.module_paths.append(os.path.join(self._emk_dir, "modules"))
        self._local.current_scope = root_scope
        
        # insert "clean" module
        self.insert_module("clean", _Clean_Module(self.scope_name))
        self.pre_modules.append("clean")
        
        self._load_config()
            
        start_time = time.time()
        self._push_scope("project", path) # need an initial "project" scope
        self._handle_dir(path, first_dir=True)

        self._done_build = False
        self._added_rule = False
        while (self._have_unbuilt() and (self._added_rule or self._prebuild_funcs or self._postbuild_funcs)) or \
          self._must_build or \
          ((not self._done_build) and (self._explicit_targets or self._auto_targets or self._prebuild_funcs or self._postbuild_funcs)):
            self._run_prebuild_funcs()
            
            self._remove_artificial_targets()
            self._fix_aliases()
            self._fix_depends()
            
            # fix up requires (set up absolute paths, and map to targets)
            for rule in self._rules:
                self._fix_requires(rule)
            
            self._fix_attached()
            self._fix_auto_targets()
            self._fix_allowed_nonexistent()
            self._fix_requires_rule()

            self._added_rule = False
            
            self._do_build()
            self._must_build = []
            
            self._run_postbuild_funcs()
            
            # recurse into any new dirs
            for scope in self._visited_dirs.values():
                if scope.recurse_dirs:
                    recurse_dirs = scope.recurse_dirs
                    scope.recurse_dirs = set()
                    
                    self._local.current_scope = scope.parent
                    for d in recurse_dirs:
                        self._handle_dir(d)
        
        self._write_modtime_caches()
        
        unbuilt = set()
        for path, target in self._targets.items():
            if target._visited and not target._built:
                unbuilt.add(target)
        
        unbuilt_lines = []
        for target in unbuilt:
            if target.rule:
                unbuilt_deps = []
                for dep in target.rule._required_targets:
                    if dep in unbuilt:
                        unbuilt_deps.append(dep.abs_path)
                unbuilt_lines.append("%s depends on unbuilt %s" % (target.abs_path, unbuilt_deps))
            else:
                unbuilt_lines.append("No rule produces %s, and it does not exist" % (target.abs_path))
        if unbuilt:
            raise _BuildError("Some targets could not be built", unbuilt_lines)
        
        diff = time.time() - start_time
        self.log.info("Finished in %0.3f seconds" % (diff))
    
    def import_from(self, paths, name):
        return self._import_from(paths, name)
    
    def insert_module(self, name, instance):
        if self.building:
            stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
            raise _BuildError("Cannot call insert_module() when building", stack)
            
        if name in self.scope.modules:
            self.log.warning("Cannot insert over pre-existing '%s' module", name)
            return None
        
        mod = _Module_Instance(name, instance, None)
        _try_call_method(mod, "load_" + self.scope.scope_type)
        self.scope.modules[name] = mod
        return instance

    def module(self, name):
        # if the module has already been loaded in the current scope, return the existing instance
        if name in self.scope.modules:
            return self.scope.modules[name].instance
        
        if self.building:
            stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
            raise _BuildError("Cannot load a new module when building", stack)
        
        # if any of the parent scopes contain an instance, return a new instance with that as a parent
        cur = self.scope.parent
        while cur:
            if name in cur.modules:
                try:
                    instance = cur.modules[name].instance.new_scope(self.scope_name)
                except _BuildError:
                    raise
                except Exception:
                    raise _BuildError("Error creating new scope for module %s" % (name), _get_exception_info())
                
                mod = _Module_Instance(name, instance, cur.modules[name].mod)
                _try_call_method(mod, "load_" + self.scope_name)
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
            mpath = os.path.realpath(os.path.abspath(pathname))
            if not mpath in self._all_loaded_modules:
                d, tail = os.path.split(mpath)
                os.chdir(d)
                self._all_loaded_modules[mpath] = imp.load_module(name, fp, pathname, description)
            
            instance = self._all_loaded_modules[mpath].Module(self.scope_name)
            mod = _Module_Instance(name, instance, self._all_loaded_modules[mpath])
            _try_call_method(mod, "load_" + self.scope_name)
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
    
    def modules(self, *names):
        mods = []
        for name in names:
            mods.append(self.module(name))
        return mods
    
    # 0-length produces and requires ("") are ignored. A require of emk.ALWAYS_BUILD means that this rule must always be built
    def rule(self, produces, requires, func, args=[], threadsafe=False):
        stack = _format_stack(_filter_stack(traceback.extract_stack()[:-1]))
        self._rule(produces, requires, args, func, threadsafe, stack)
    
    # decorator for "easy" rule creation
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

    # decorator for "easy" rule creation
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
    
    # decorator for "easy" rule creation
    def args(self, *args):
        def decorate(f):
            if isinstance(f, _RuleWrapper):
                f.args = args
                return f
            else:
                stack = _format_decorator_stack(_filter_stack(traceback.extract_stack()[:-1]))
                wrapper = _RuleWrapper(f, stack, args=args)
                self.scope._wrapped_rules.append(wrapper)
                return wrapper
        return decorate

    # decorator for "easy" rule creation
    def threadsafe(self, f):
        if isinstance(f, _RuleWrapper):
            f.threadsafe = True
            return f
        else:
            stack = _format_decorator_stack(_filter_stack(traceback.extract_stack()[:-1]))
            wrapper = _RuleWrapper(f, stack, threadsafe=True)
            self.scope._wrapped_rules.append(wrapper)
            return wrapper
    
    def depend(self, target, *dependencies):
        fixed_depends = [_make_require_abspath(d, self.scope) for d in dependencies if d != ""]
        if not fixed_depends:
            return
        
        abspath = _make_target_abspath(target, self.scope)
        self.log.debug("Adding %s as dependencies of target %s", fixed_depends, abspath)
        with self._lock:
            if abspath in self._secondary_dependencies:
                self._secondary_dependencies[abspath].extend(fixed_depends)
            else:
                self._secondary_dependencies[abspath] = list(fixed_depends)
    
    def attach(self, target, *attached_targets):
        fixed_depends = [_make_require_abspath(d, self.scope) for d in attached_targets if d != ""]
        abspath = _make_target_abspath(target, self.scope)
        self.log.debug("Attaching %s to target %s", fixed_depends, abspath)
        with self._lock:
            if abspath in self._attached_dependencies:
                self._attached_dependencies[abspath].extend(fixed_depends)
            else:
                self._attached_dependencies[abspath] = list(fixed_depends)
    
    def build(self, *targets):
        with self._lock:
            for target in targets:
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
    
    def allow_nonexistent(self, *paths):
        with self._lock:
            for path in paths:
                abs_path = _make_require_abspath(path, self.scope)
                self.log.debug("Allowing %s to not exist", abs_path)
                self._allowed_nonexistent.add(abs_path)
    
    def require_rule(self, *paths):
        with self._lock:
            for path in paths:
                abs_path = _make_require_abspath(path, self.scope)
                self.log.debug("Requiring %s to be built by an explicit rule", abs_path)
                self._requires_rule.add(abs_path)
    
    def recurse(self, *paths):
        for path in paths:
            abspath = _make_target_abspath(path, self.scope)
            self.log.debug("Adding recurse directory %s", abspath)
            self.scope.recurse_dirs.add(abspath)
    
    def subdir(self, *paths):
        self.recurse(*paths)
        sub_cleans = [os.path.join(path, "clean") for path in paths]
        self.attach("clean", *sub_cleans)
    
    def do_later(self, func):
        self.scope._do_later_funcs.append(func)
    
    def do_prebuild(self, func):
        with self._lock:
            self._prebuild_funcs.append((self.scope, func))
    
    def do_postbuild(self, func):
        with self._lock:
            self._postbuild_funcs.append((self.scope, func))
    
    def mark_exists(self, *paths):
        with self._lock:
            for path in paths:
                self.log.debug("Marking %s as existing", path)
                abs_path = _make_target_abspath(path, self.scope)
                if abs_path in self._modtime_cache:
                    self._modtime_cache[abs_path][0] = True
                else:
                    cache = [True, 0, {}]
                    self._modtime_cache[abs_path] = cache
                    self.scope.modtime_cache[abs_path] = cache
    
    def mark_untouched(self, *paths):
        with self._lock:
            for path in paths:
                abs_path = _make_target_abspath(path, self.scope)
                if abs_path in self._targets:
                    self.log.debug("Marking %s as untouched", abs_path)
                    self._targets[abs_path]._untouched = True
                else:
                    self.log.warning("Not marking %s as untouched since it is not a target", abs_path)
    
    def log_print(self, format, *args):
        d = {'adorn':False}
        self.log.info(format, *args, extra=d)
    
    def abspath(self, path):
        return _make_target_abspath(path, self.scope)
    
    def resolve_build_dirs(self, *paths, **kwargs):
        ignore_errors = False
        if "ignore_errors" in kwargs and kwargs["ignore_errors"]:
            ignore_errors = True
        return self._resolve_build_dirs(paths, ignore_errors)
    
    def fix_stack(self, stack):
        return _format_stack(_filter_stack(stack))
    
    def cached_data(self, path):
        with self._lock:
            abs_path = _make_target_abspath(path, self.scope)
            if not abs_path in self._modtime_cache:
                cache = [False, 0, {}]
                self._modtime_cache[abs_path] = cache
                self.scope.modtime_cache[abs_path] = cache
            return self._modtime_cache[abs_path][2]

def setup(argv=[]):
    emk = EMK(argv)
    builtins.emk = emk
    return emk

def main(args):
    try:
        setup(args).run(os.getcwd())
        return 0
    except _BuildError as e:
        print("Build error: %s" % (e), file=sys.stderr)
        if e.extra_info:
            for line in e.extra_info:
                print("    %s" % (line), file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
