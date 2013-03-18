emk Manual
==========

emk is a Python script that gathers build rules from various config files and executes them depending on the user-specified targets.

Arguments
---------
`emk target1 option1=val target2 ....`

Arguments to emk can either be options or targets. An option is an argument of the form "key=value". Any arguments that do not contain '=' are treated
as explicit targets to be built. You may specify targets that contain '=' using the special option "explicit_target=<target name>". All options (whether
or not they are recognized by emk) can be accessed via the emk.options dict.
        
If no explicit targets are specified, emk will build all autobuild targets.

Recognized options:
```
  log     -- The log level that emk will use. May be one of ["debug", "info", "warning", "error", "critical"], 
             although error and critical are probably not useful. The default value is "info".
  emk_dev -- If set to "yes", developer mode is turned on. Currently this disables stack filtering so
             that errors within emk can be debugged. The default value is "no".
  threads -- Set the number of threads used by emk for building. May be either a positive number, or "x".
             If the value is a number, emk will use that many threads for building; if the value is "x",
             emk will use as many threads as there are cores on the build machine. The default value is "x".
  style   -- Set the log style mode. May be one of ["no", "console", "html", "passthrough"]. If set to "no",
             log output styling is disabled. If set to "console", ANSI escape codes will be used to color log
             output (not yet supported on Windows). If set to "html", the log output will be marked up with &lt;div>
             and &lt;span> tags that can then be styled using CSS. If set to "passthrough", the style metadata will
             be output directly (useful if emk is calling itself as a subprocess). The default value is "console".
  trace   -- Specify a set of targets to trace for debugging purposes. The trace for each target will be printed
             once the build is complete. The targets are specified as a list of comma-separated paths, which may be
             relative to the current directory or absolute. Build and project directory placeholders will be
             replaced based on the current directory.
  trace_unchanged -- If set to "yes", the tracer will trace through targets that were not modified as well.
                     The default value is "no".
```

Note that you can pass in other options that may be interpreted by the various config files.

Scopes
------

With emk, you can specify configuration at a global or project level, and then override that configuration for specific directories.
The configuration system is based on 'scopes'; scopes apply to certain emk configuration properties, as well as the module system.
The basic idea is when you enter a new scope, configuration from the parent scope is copied into the new scope; it can then be modified
in the new scope to override configuration as desired without changing the parent scope's configuration.

When you load a module in a given scope, emk will see if that module has been loaded in a parent scope. If it has, the module for the
current scope will be initialized from the parent scope's module (otherwise the module will be initialized to its default settings).
This behaviour is module-specific but typically involves copying the configuration values set in the parent scope's module to the new module instance.

emk maintains a per-scope cache (rules scope only) that can be retrieved and modified using `emk.scope_cache(key)`. The cache can be used to
store information between emk invocations (for example, the discovered header file dependencies for a C file). The cache can only be retrieved
and modified when you are in rules scope.

Loading Sequence
----------------

The build process in a given directory goes as follows:
  1. Load the global emk config from `<emk dir>/config/emk_global.py` (where <emk dir> is the directory containing the emk.py module),
     if it exists and has not already been loaded (creates the global/root scope). Whenever emk loads any config file, it changes its
     working directory to the directory containing the config file. Note that the global config file may be a symlink.
  2. Find the project directory. The project directory is the closest ancestor to the current directory that
     contains an `emk_project.py` file, or the root directory if no project file is found. The project directory for the current directory
     is available via `emk.proj_dir`.
  3. Load the project file `emk_project.py` from the project directory if it exists and has not already been loaded (creates a new scope, with the global scope as a parent).
  4. For each directory from the project directory to the current directory, load `emk_subproj.py` from that directory
     if it exists and has not already been loaded (creates a new scope, with the previous scope as a parent).
  5. Create the rules scope for the current directory (creates a new scope).
  6. Load any premodules (specified via appending to the `emk.pre_modules` list).
  7. Load `emk_rules.py` from the current directory if it exists; otherwise, load the default modules (if any; specified by appending to the `emk.default_modules` list).
  8. Run any module post_rules() methods for modules loaded into the rules scope.
  9. Recurse into any directories (specified using `emk.recurse()` or `emk.subdir()`) that have not already been visited.

Once there are no more directories to recurse into, the prebuild functions are executed until there aren't any more.
Prebuild functions specified during the prebuild stage are executed after all of the previous prebuild functions
have been executed. Prebuild functions are specified using `emk.do_prebuild()`. Note that if a prebuild function specifies
a new directory to recurse into, emk will handle that directory immediately after the function has been executed.

Then, the first build phase starts. If explicit targets have been specified and they can all be resolved, only those
targets (and their dependencies) are examined. Otherwise, all autobuild targets (and their dependencies) are examined.
Examined targets will be run if the dependencies have changed (or if the products have changed and have been declared
as rebuild_if_changed).

Building continues until everything that can be built (from the set of examined targets) has been built. Note that it is
possible that not all examined targets could be built immediately, since they may depend on things for which rules have
not yet been declared. emk will attempt to build those targets later.

Once building is complete, the postbuild functions are executed. Postbuild functions are specified using `emk.do_postbuild()`.
Note that if new postbuild functions are added during the postbuild stage, they will not be executed until after the next build phase.

Finally, any new directories are recursed into. If there is still work left to do (ie, unbuilt targets), emk will start
a new build phase (returning to the prebuild stage). Build phases will continue until all targets are built, or until
there is nothing left to do. If there are unbuilt targets after building has stopped, a build error is raised.

Build Directory
---------------

emk has a configurable build directory. This is used to store emk's cache, and is also where build products (from the supplied modules)
are put. By default, the build directory is a relative path ("__build__"); this means that the cache and build products for a given directory
that is being built (ie, a directory containing an `emk_rules.py` file) will be put into an "__build__" subdirectory of that directory.
The build directory may also be an absolute path, in which case build products for multiple directories may be put into that directory.

The build directory is a scoped property of emk (`emk.build_dir`). This means that you can modify it in `emk_global.py`, `emk_project.py`,
or `emk_subproj.py`. However you cannot change the build directory in `emk_rules.py` - this is to make it consistent for a given directory.

Note that if you make the build directory an absolute path (or otherwise shared by multiple directories), there may be name conflict issues
from rules generated by modules (for example, if two different directories both have a file "example.c", the c module will create "example.o"
for both of those by default). To fix this, the provided c, link, and java modules have a `unique_names` property that will add the directory
path (relative to the project directory) to autogenerated file names to prevent name conflicts.

emk Object
----------

Whenever emk is running, the `emk` object is available as a builtin. You do not need to (and should not) try to import emk in your emk modules
or config files; you can just use emk.<whatever> directly.

### Global read-only properties (not based on current scope):
```
  log          -- The emk log (named 'emk'). Modules should create sub-logs of this to use the emk logging features.
  formatter    -- The formatter instance for the emk log.
  ALWAYS_BUILD -- A special token. When used as a rule requirement, ensures that the rule will always be executed.
  cleaning     -- True if "clean" has been passed as an explicit target; false otherwise.
  building     -- True when rules are being executed, false at other times.
  emk_dir      -- The directory which contains the emk module.
  options      -- A dict containing all command-line options passed to emk (ie, arguments of the form key=value).
                  You can modify the contents of this dict.
  explicit_targets -- The set of explicit targets passed to emk (ie, all arguments that are not options).
  traces       -- The set of targets that will be traced once the build is complete (for debugging).
```

### Global modifiable properties:
```
  default_has_changed   -- The default function to determine if a rule requirement or product has changed.
                           If replaced, the replacement function should take a single argument which is the absolute
                           path of the thing to check to see if it has changed. When this function is executing,
                           emk.current_rule and emk.rule_cache() are available.
  build_dir_placeholder -- The placeholder to use for emk.build_dir in paths passed to emk functions.
                           The default value is "$:build:$".
  proj_dir_placeholder  -- The placeholder to use for emk.proj_dir in paths passed to emk functions.
                           The default value is "$:proj:$".
```

### Scoped read-only properties (apply only to the current scope):
```
  scope_name    -- The name of the current scope. May be one of ['global', 'project', 'subproj', 'rules].
  proj_dir      -- The absolute path of the project directory for the current scope.
  scope_dir     -- The absolute path of the directory in which the scope was created
                   (eg, the directory from which the emk_<scope name>.py file was loaded).
  local_targets -- The dict of potential targets (ie, rule products) defined in the current scope.
                   This maps the original target path (ie, as passed into emk.rule() or @emk.make_rule) to
                   the emk.Target instance.
  current_rule  -- The currently executing rule (an emk.Rule instance), or None if a rule is not being executed.
```
  
### Scoped modifiable properties (inherited by child scopes):
```
  build_dir       -- The build directory path (may be relative or absolute). The default value is "__build__".
  module_paths    -- Additional absolute paths to search for modules.
  default_modules -- Modes that are loaded if no emk_rules.py file is present.
  pre_modules     -- Modules that are preloaded before each emk_rules.py file is loaded.
```

Modules
-------

emk has a module system which allows automatic creation of rules, and easy hierarchical configuration.
When a module is loaded into a scope, emk will check to see if the module is already present in the scope;
if it is, then the module instance is returned. Otherwise, emk will try to find an instance of the module
in a parent scope. If a parent instance is found, a new instance is created for the current scope using the
parent instance's new_scope() method. This allows the new module instance to inherit configuration values
from the parent scope if desired (based on how the module was designed).

If the module is not present in any parent scope, emk will try to load a Python module of the same name from
the scope's module search paths (`emk.module_paths`). Note that the module search paths may be relative;
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

### Loading Modules

To load a module (or multiple modules) at any time (except when executing a rule), use `emk.module(names)`:

Arguments:
```
  names -- The list of modules names (or a single name) to load into the current scope.
```

`emk.module(names)` returns the list of module instances corresponding to the given module names; None will be in the list for each module
that could not be loaded. If only one name is provided, the result will be a value rather than a list (for convenience,
so that you can write `mymod = emk.module("my_module")`, but also write `c, link = emk.module("c", "link")`).

You can also use `emk.weak_module` to load one or emk modules into the current scope, without causing their post_<scope type>() methods to be called.

### Inserting Modules

Using `emk.insert_module(name, instance)`, you can an emk module instance into the current scope as a weak module.
This method allows you to create a module instance and provide it for use by child scopes without needing to
create an actual Python module file to import. The instance will be installed into the current scope as a weak
module, so the current scope can also load it using emk.module() if desired after it has been inserted.

When the module instance is being inserted, its load_<scope type>() method will be called, if present. If a module
instance of the same name already exists in the current scope (either as a normal module or weak module), a build
error will be raised; however you can insert a module that will override a module in any parent scope (or a Python module)
as long as the current scope has not yet loaded it.

Arguments:
```
  name     -- The name of the module being inserted (as would be passed to `emk.module()`)
  instance -- The module instance to insert.
```

Cleaning
--------

emk has a built-in `clean` module that automatically creates a "clean" target. If you specify "clean" as a target when you call emk (`emk clean`),
then `emk.cleaning` will be set to True, and no other explicit targets will be built.

By default, when `emk clean` is called in a directory, the rule to make "clean" will remove the build directory for that directory if the build directory
is a subdirectory of that directory (ie, is a relative path that does not use ".." or symlinks to point to something outside of the given directory).
If the build directory is not a subdirectory, then `emk clean` will only delete the emk cache for that directory.

You can change the configurable `clean.remove_build_dir` property of the `clean` module to False to prevent removing the build directory in all cases
(for the configured scope).

If you use `emk.subdir(path)` to instruct emk to recurse into other directories, `emk clean` will clean those directories as well. Directories specified
using only `emk.recurse(path)` will not be cleaned. You can use the utils module `utils.clean_rule()` method to specify additional file patterns
to be removed when cleaning.

You can attach targets to the "clean" target (using `emk.attach()`) to perform other tasks when `emk clean` is called. This is how `utils.clean_rule()`
and `emk.subdir()` work.

Targets and Dependencies
------------------------

As in most build systems, emk rules specify sets of products (that the rules produce when executed) and dependencies (that
must be up-to-date before the rule can be executed). When building, emk attempts to make the required set of targets (either specified
on the command line, or autobuild targets) up-to-date by walking the dependency graph from the targets (as prducts of rules) back to files
that exist but have no rules to make them. Then, any rules whose dependencies have changed will be executed (in parallel if possible)
until all the required targets have been produced.

emk handles all targets and dependencies as absolute (canonical) paths internally; they may be specified as relative paths in the config files
for convenience. You may also use the project and build directory placeholders (`$:proj:$` and `$:build:$` by default) in target and
dependency paths; emk will resolve the placeholders as appropriate before the paths are passed to a rule function.

By default, emk caches the modification time of each file; if the modification time of a file is different from the cached value
then the file is considered to be changed. This method of determining if something has changed can be modified on a global basis
(for all rules) by setting `emk.default_has_changed`, or for a single rule by passing in the `has_changed` keyword argument
to `emk.rule()` or `@emk.make_rule()`.

Build Rules
----------------------

Build rules may only be specified when in rules scope. This means that you can specify build rules in an `emk_rules.py` file,
in a module's pre_rules() function or post_rules() function, or in any do_later/prebuild/postbuild function specified in rules scope.

A build rule is the combination of a rule function and a set of arguments to pass to that function (since build rules are not executed
until the build stage). A build rule specifies a list of things it produces, and a list of things it requires (as well as the additional
arguments that will be passed to the rule function, if any). emk will ensure that all the requirements in the requires list (the primary
dependencies) have been built or otherwise exist before the rule function is executed. You may add additional dependencies to a build rule
at any time (even before it has been created) using `emk.depend()`.

A rule function is a function that takes at least two arguments: a list of things that it must produce, and a list of things that it
depends on. The function may take other arguments if desired. When the function is executed, it must ensure that all declared products are actually
produced (they must be either present in the filesystem, or declared virtual using `emk.mark_virtual()`). When the build rule is specified,
the list of productions and requirements may contain both relative and absolute paths; emk will convert everything to absolute paths before
passing them to the rule function.

Rules may be declared as either cwd-safe or cwd-unsafe (using the cwd_safe keyword argument).
cwd-safe rules may be executed in parallel and must not depend on the current working directory.
cwd-unsafe rules are all executed by a single thread; the current working directory will be set to
the scope directory that the rule was created in (eg, the directory containing emk_rules.py) before the rule is executed.

It is a build error to declare more than one rule that produces the same target.

### Specifying Build Rules

If you have an existing rule function and you want to specify a build rule that uses that function, you should use
`emk.rule(func, produces, requires, *args, **kwargs)`

Arguments:
```
  func     -- The rule function to execute. Must take the correct number of arguments (produces, requires, and
              the additional args).
  produces -- List of paths that the rule produces. The paths may be absolute, or relative to the scope dir.
              Project and build dir placeholders will be resolved according to the current scope.
              Empty paths ("") are ignored. This argument will be converted into a list of canonical paths, and
              passed as the first argument to the rule function.
  requires -- List of paths that the rule requires to be built before it can be executed (ie, dependencies).
              The paths may be absolute, or relative to the scope dir. Project and build dir placeholders will
              be resolved according to each path. Empty paths ("") are ignored. May include the special
              emk.ALWAYS_BUILD token to indicate that the rule should always be executed. This argument will be
              converted into a list of canonical paths, and passed as the second argument to the rule function.
  args     -- Additional arguments that will be passed to the rule function.
  kwargs   -- Keyword arguments - see below.
```

Keyword arguments:
```
  cwd_safe    -- If True, the rule is considered to be cwd-safe (ie, does not depend on the current working
                 directory). The default value is False.
  ex_safe     -- If False, then emk will print a warning message if the execution of the rule is interrupted
                 in any way. The warning indicates that the rule was partially executed and may have left partial
                 build products, so the build should be cleaned. The default value is False.
  has_changed -- The function to execute for this rule to determine if the dependencies (or "rebuild if changed"
                 products) have changed. The default value is `emk.default_has_changed`.
```

If you have a one-off build rule, you may want to use a decorator on the rule function instead, using
`@emk.make_rule(produces, requires, *args, **kwargs)`. The arguments are the same as for `emk.rule()`, except the rule function
is the function being decorated.

Rule Functions
--------------

When a rule function is executed by emk, it is passed a list of paths that must be produced (as the first argument).
After the rule function has executed, emk will check to make sure that everything that was supposed to be produced
actually exists; if something does not exist, a build error is raised. "Exists" in this context means that either
the file with the given path exists in the filesystem, or the product path has been declared as "virtual" using
`emk.mark_virtual(*paths)`. The virtual path system allows you to have targets that do not create actual files
(eg the "clean" target).

As a rule is executing, it may discover that some or all of its products do not actually need to be updated.
In this case, the rule function should mark those products as untouched using `emk.mark_untouched(*paths)`. This will
prevent unnecessary execution of rules that depend on the unmodified products. Note that this currently is only required
for virtual products; for real files, you can achieve the same effect by not modifying the file.

emk maintains a per-rule cache for things like modification times. This cache can be used by the rule function to store information
between rule invocations, using `emk.rule_cache(key)`. The cache can only be retrieved when a rule is executing. The returned cache
is a dict that can be modified to store data for the next time the rule is run. This could be used (for example) to store information
to determine whether a rule product needs to be updated or can be marked as untouched.

Modifying Targets
-----------------

Anything produced by a build rule is a potential target. emk offers several functions to change its behaviour with respect to a given target.

### Adding Dependencies

You can add dependencies to a target (in addition to the requirements of the rule that produces the target) using `emk.depend(target, *dependencies)`.
Before executing a rule, emk will ensure that all dependencies of all targets that the rule produces are up-to-date.

To support dependencies that are discovered during the build process (like header files in C/C++), emk provides the `emk.weak_depend(target, *dependencies)`
function. This specifies additional dependencies of the target as well, but weak dependencies are allowed to not exist (if a weak dependency does not
exist, and there is no rule to build it, the build can continue as if there was no dependency).

### Attaching Targets

In some cases, you may want to ensure that a given target is produced whenever another target is built, but there is no dependency relationship.
For example, you might want to perform some additional cleanup tasks whenever the "clean" target is built (ie, `emk clean`). To do this, use
`emk.attach(target, *attached_targets)`. emk will ensure that if the given target is built (ie, its build rule is executed), the attached targets will
also be built at some point. Note that there is no ordering of build rule execution implied by attaching one target to another.

### Autobuild Targets

If no explicit targets are passed in on the command line, emk will build all targets that have been
marked as autobuild. emk will also build all autobuild targets when the explicit targets cannot be
fully built due to missing rules or dependencies. To mark one or more targets as autobuild, use `emk.autobuild(*targets)`.

### Target Aliases

In some cases, the actual generated file name will be annoying to specify manually (eg as an explicit target). To alleviate this issue,
you can create an alias for a target using `emk.alias(target, alias)`. This allows the target to be referred to by the alias path as well
as the original target path.

If there is an existing alias with the same canonical path, or a rule is ever declared to produce a target
with the same path, a build error will be raised. Aliases may refer to other aliases. Aliases may also refer to normal files that are not products of any rule.

### Rebuild if Changed

In some cases you way want to rebuild a given target if it has been modified since the last build (eg manually), even if its dependencies have not
changed. To do this, use `emk.rebuild_if_changed(*paths)`. The given product paths will be assessed to see if they have changed using the has_changed function
of the rules that produce them.

### Require Rule

If there is more than 1 build phase, a rule in phase 1 might be defined as depending on things that are only produced
by rules in phase 2 or later (for example). Normally, if a rule depends on a file with no rule to make it, the rule
can be run as long as the file exists. However, if that file would be updated by a rule defined in a later build phase,
the rule that depends on it should not be run until after that later rule has been defined (and executed, if required).

By requiring those dependencies to be produced by a rule, the build process will execute correctly - emk will wait
until the later build phase has defined and executed the rule(s) that produce the dependencies before examining
the rules that depend on them. To do this, use `emk.require_rule(*paths)` for the relevant paths.

Debugging the Build
-------------------

If you call `emk log=debug`, you will get a lot of additional information about the build process and what emk is doing, including which
files have changed and why emk is executing rules.

### Traces

If you want to debug a specific target (to see why it is/isn't being built), you can trace it by using `emk trace=<target path(s)>`.
You can pass in multiple targets to trace separated by ','. You can also programmatically add traces in any emk config file
(eg `emk_rules.py`) using the `emk.trace(*paths)` function. Once the build is complete, emk will print out a trace for each traced target.
The trace includes which rules depend on that target, and the dependency tree for the target. Changed dependencies will be output in red
(depending on the log style).

By default, the dependency tree trace will not follow unchanged files. If you want to force the tracer to trace unchanged files, pass the
trace_unchanged=yes option to emk. Example: `emk trace=myprogram trace_unchanged=yes`.
