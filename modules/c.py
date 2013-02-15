import os
import logging
import shlex

log = logging.getLogger("emk.c")

utils = emk.module("utils")

class _GccCompiler(object):
    """
    Compiler class for using gcc/g++ to compile C/C++ respectively.
    
    In order for the EMK c module to use a compiler instance, the compiler class must define the following methods:
      load_extra_dependencies
      compile_c
      compile_cxx
    See the documentation for those functions in this class for more details.
    
    Properties (defaults set based on the path prefix passed to the constructor):
      c_path   -- The path of the C compiler (eg "gcc").
      cxx_path -- The path of the C++ compiler (eg "g++").
    """
    def __init__(self, path_prefix=""):
        """
        Create a new GccCompiler instance.
        
        Arguments:
          path_prefix -- The prefix to use for the gcc/g++ executables. For example, if you had a 32-bit Linux cross compiler
                         installed into /cross/linux, you might use 'c.compiler = c.GccCompiler("/cross/linux/bin/i686-pc-linux-gnu-")'
                         to configure the c module to use the cross compiler. The default value is "" (ie, use the system gcc/g++).
        """
        self.c_path = path_prefix + "gcc"
        self.cxx_path = path_prefix + "g++"
    
    def load_extra_dependencies(self, path):
        """
        Load extra dependencies from a dependency file that was written by this compiler instance's
        compile_c() or compile_cxx() methods.
        
        Arguments:
          path -- The path to the dependency file.
        
        Returns a list of paths (strings) of all the extra dependencies.
        """
        try:
            with open(path) as f:
                items = [s for s in f.read().split('\n') if s]
                return items
        except IOError:
            pass
    
    def depfile_args(self, dep_file):
        """
        Returns a list of arguments to write secondary dependencies to the given dep_file path.
        """
        return ["-Wp,-MMD,%s" % (dep_file)]
    
    def compile(self, exe, source, dest, dep_file, includes, defines, flags):
        args = [exe]
        args.extend(self.depfile_args(dep_file))
        args.extend(["-I%s" % (emk.abspath(d)) for d in includes])
        args.extend(["-D%s=%s" % (key, value) for key, value in defines.items()])
        args.extend(utils.flatten(flags))
        args.extend(["-o", dest, "-c", source])
        utils.call(args)
        
        try:
            with open(dep_file, "r+") as f:
                data = f.read()
                data = data.replace("\\\n", "")
                items = shlex.split(data)
                unique_items = set(items[2:]) - set([""])
                # set up cache for weak dependencies
                for item in unique_items:
                    emk.current_rule.has_changed_func(emk.abspath(item), emk.rule_cache(item))
                f.seek(0)
                f.truncate(0)
                f.write('\n'.join(unique_items))
        except IOError:
            log.error("Failed to fix up depfile %s", dep_file)
            utils.rm(dep_file)
        
    def compile_c(self, source, dest, dep_file, includes, defines, flags):
        """
        Compile a C source file into an object file.
        
        Arguments:
          source   -- The C source file path to compile.
          dest     -- The output object file path.
          dep_file -- The path to a dep file that should be written to contain secondary dependencies of the object file
                      (eg, header files (transitively) included from the source file). This file will be read by
                      the compiler's load_extra_dependencies() method.
          includes -- A list of extra include directories.
          defines  -- A dict of <name>: <value> entries to be used as defines; each entry is equivalent to #define <name> <value>.
          flags    -- A list of additional flags. This list may contain tuples; to flatten the list, you could use the EMK utils module:
                      'flattened = utils.flatten(flags)'.
        """
        self.compile(self.c_path, source, dest, dep_file, includes, defines, flags)
    
    def compile_cxx(self, source, dest, dep_file, includes, defines, flags):
        """
        Compile a C++ source file into an object file.
        
        Arguments:
          source   -- The C++ source file path to compile.
          dest     -- The output object file path.
          dep_file -- The path to a dep file that should be written to contain secondary dependencies of the object file
                      (eg, header files (transitively) included from the source file). This file will be read by
                      the compiler's load_extra_dependencies() method.
          includes -- A list of extra include directories.
          defines  -- A dict of <name>: <value> entries to be used as defines; each entry is equivalent to #define <name> <value>.
          flags    -- A list of additional flags. This list may contain tuples; to flatten the list, you could use the EMK utils module:
                      'flattened = utils.flatten(flags)'.
        """
        self.compile(self.cxx_path, source, dest, dep_file, includes, defines, flags)

class Module(object):
    """
    EMK module for compiling C and C++ code. Depends on the link module (and utils).
    
    This module defines EMK rules during the prebuild stage, to allow autodiscovery of generated source files
    from rules defined before the prebuild stage (ie, in the post_rules() method of other modules). See the
    autodetect and autodetect_from_targets properties for more information about autodiscovery of source files.
    
    This module adds the compiled object files to the link module, which will link them into libraries/executables as desired.
    The object files are added to the link module's 'objects' property (each mapped to the source file that the object file
    was built from), so that the link module can autodetect main() functions from the source (if link.detect_exe == "simple").
    See the link module documentation for details of main() autodetection.
    
    The c module also sets the link module's link_cxx flag if there are any C++ source files being compiled.
    
    Note that the compilation rules are not built automatically; the link module (or other modules/user code)
    is responsible for marking the object files as autobuild if desired.
    
    Classes:
      GccCompiler -- A compiler class that uses gcc/g++ to compile.
    
    Properties (inherited from parent scope):
      compiler     -- The compiler instance that is used to load dependencies and compile C/C++ code.
      include_dirs -- A list of additional include directories for both C and C++ code.
      defines      -- A dict of <name>: <value> defines for both C and C++; each entry is equivalent to #define <name> <value>.
      flags        -- A list of flags for both C and C++. If you have a 'flag' that is more than one argument, pass it as a tuple.
                      Example: ("-isystem", "/path/to/extra/sys/includes"). Duplicate flags will be removed.
      source_files -- A list of files that should be included for compilation. Files will be built as C or C++ depending on the file extension.
      
      c.exts         -- The list of file extensions (suffixes) that will be considered as C code. The default is [".c"].
      c.include_dirs -- A list of additional include directories for C code.
      c.defines      -- A dict of <name>: <value> defines for C.
      c.flags        -- A list of flags for C.
      c.source_files -- A list of C files that should be included for compilation (will be built as C code).
      
      cxx.exts         -- The list of file extensions (suffixes) that will be considered as C++ code. The default is [".cpp", ".cxx", ".c++", ".cc"].
      cxx.include_dirs -- A list of additional include directories for C++ code.
      cxx.defines      -- A dict of <name>: <value> defines for C++.
      cxx.flags        -- A list of flags for C++.
      cxx.source_files -- A list of C++ files that should be included for compilation (will be built as C++ code).
      
      autodetect  -- Whether or not to autodetect files to build from the scope directory. All files that match the c.exts suffixes
                     will be compiled as C, and all files that match the cxx.exts suffixes will be compiled as C++. Autodetection
                     does not take place until the prebuild stage, so that autodetection of generated code can gather as many targets
                     as possible (see autodetect_from_targets). The default value is True.
      autodetect_from_targets -- Whether or not to autodetect generated code based on rules defined in the current scope.
                                 The default value is True.
      excludes    -- A list of source files to exclude from compilation.
      non_lib_src -- A list of source files that will not be linked into a library for this directory (passed to the link module).
      non_exe_src -- A list of source files that will not be linked into an executable, even if they contain a main() function.
    """
    def __init__(self, scope, parent=None):
        self.GccCompiler = _GccCompiler
        
        self.link = emk.module("link")
        self.c = emk.Container()
        self.cxx = emk.Container()
        
        if parent:
            self.compiler = parent.compiler
            
            self.include_dirs = list(parent.include_dirs)
            self.defines = parent.defines.copy()
            self.flags = list(parent.flags)
            
            self.source_files = list(parent.source_files)

            self.c.exts = list(parent.c.exts)
            self.c.include_dirs = list(parent.c.include_dirs)
            self.c.defines = parent.c.defines.copy()
            self.c.flags = list(parent.c.flags)
            self.c.source_files = list(parent.c.source_files)
            
            self.cxx.exts = list(parent.cxx.exts)
            self.cxx.include_dirs = list(parent.cxx.include_dirs)
            self.cxx.defines = parent.cxx.defines.copy()
            self.cxx.flags = list(parent.cxx.flags)
            self.cxx.source_files = list(parent.cxx.source_files)
            
            self.autodetect = parent.autodetect
            self.autodetect_from_targets = parent.autodetect_from_targets
            self.excludes = list(parent.excludes)
            self.non_lib_src = list(parent.non_lib_src)
            self.non_exe_src = list(parent.non_exe_src)
        else:
            self.compiler = _GccCompiler()
            
            self.include_dirs = []
            self.defines = {}
            self.flags = []
            
            self.source_files = []
            
            self.c.include_dirs = []
            self.c.defines = {}
            self.c.flags = []
            self.c.exts = [".c"]
            self.c.source_files = []
            
            self.cxx.include_dirs = []
            self.cxx.defines = {}
            self.cxx.flags = []
            self.cxx.exts = [".cpp", ".cxx", ".c++", ".cc"]
            self.cxx.source_files = []
        
            self.autodetect = True
            self.autodetect_from_targets = True
            self.excludes = []
            self.non_lib_src = []
            self.non_exe_src = []
    
    def new_scope(self, scope):
        return Module(scope, parent=self)
    
    def _matches_exts(self, file_path, exts):
        for ext in exts:
            if file_path.endswith(ext):
                return True
        return False
    
    def post_rules(self):
        if emk.cleaning:
            return
        
        emk.do_prebuild(self._prebuild)
    
    def _prebuild(self):
        c_sources = set()
        cxx_sources = set()
        
        self._non_exe_src = set(self.non_exe_src)
        self._non_lib_src = set(self.non_lib_src)
        
        if self.autodetect:
            if self.autodetect_from_targets:
                target_c_files = [t for t in emk.local_targets.keys() if self._matches_exts(t, self.c.exts)]
                if target_c_files:
                    log.debug("Detected generated C files: %s", target_c_files)
                    self.c.source_files.extend(target_c_files)
                    
                target_cxx_files = [t for t in emk.local_targets.keys() if self._matches_exts(t, self.cxx.exts)]
                if target_cxx_files:
                    log.debug("Detected generated C++ files: %s", target_cxx_files)
                    self.cxx.source_files.extend(target_cxx_files)
                    
            files = set(self.source_files)
            files.update([f for f in os.listdir(emk.scope_dir) if os.path.isfile(f)])
            for file_path in files:
                if self._matches_exts(file_path, self.c.exts):
                    self.c.source_files.append(file_path)
                if self._matches_exts(file_path, self.cxx.exts):
                    self.cxx.source_files.append(file_path)
        
        for f in self.c.source_files:
            if f in self.excludes:
                continue
            c_sources.add(f)
        for f in self.cxx.source_files:
            if f in self.excludes:
                continue
            cxx_sources.add(f)
        
        c_includes = utils.unique_list(self.include_dirs + self.c.include_dirs)
        c_flags = utils.unique_list(self.flags + self.c.flags)
        c_defines = dict(self.defines)
        c_defines.update(self.c.defines)
        c_args = (False, c_includes, c_defines, c_flags)
        
        cxx_includes = utils.unique_list(self.include_dirs + self.cxx.include_dirs)
        cxx_flags = utils.unique_list(self.flags + self.cxx.flags)
        cxx_defines = dict(self.defines)
        cxx_defines.update(self.cxx.defines)
        cxx_args = (True, cxx_includes, cxx_defines, cxx_flags)
        
        objs = {}
        for src in c_sources:
            self._add_rule(objs, src, c_args)
        for src in cxx_sources:
            self._add_rule(objs, src, cxx_args)
        
        if self.link:
            self.link.objects.update([(os.path.join(emk.build_dir, obj + ".o"), src) for obj, src in objs.items()])
            if cxx_sources:
                self.link.link_cxx = True
    
    def _add_rule(self, objs, src, args):
        fname = os.path.basename(src)
        n, ext = os.path.splitext(fname)
        name = n
        c = 1
        while name in objs:
            name = "%s_%s" % (n, c)
            c += 1
        objs[name] = src
        if self.link:
            objpath = os.path.join(emk.build_dir, name + ".o")
            if src in self._non_exe_src:
                self.link.non_exe_objs.append(objpath)
            if src in self._non_lib_src:
                self.link.non_lib_objs.append(objpath)
        
        dest = os.path.join(emk.build_dir, name + ".o")
        requires = [src]
        extra_deps = None
        if self.compiler:
            extra_deps = self.compiler.load_extra_dependencies(emk.abspath(dest + ".dep"))
        if extra_deps is None:
            requires.append(emk.ALWAYS_BUILD)
        
        emk.rule(self.do_compile, [dest], requires, *args, threadsafe=True, ex_safe=True)
        if extra_deps:
            emk.weak_depend(dest, extra_deps)
    
    def do_compile(self, produces, requires, cxx, includes, defines, flags):
        """
        Rule function to compile a source file into an object file.
        
        The compiler instance will also produce an <object file>.dep file that contains additional dependencies (ie, header files).
        
        Arguments:
          produces -- The path to the object file that will be produced.
          requires -- The list of dependencies; the source file should be first.
          cxx      -- If True, the source file will be compiled as C++; otherwise it will be compiled as C.
          includes -- A list of additional include directories.
          defines  -- A dict of <name>: <value> entries to be defined (like #define <name> <value>).
          flags    -- A list of flags to pass to the compiler. Compound flags should be in a tuple, eg: ("-isystem", "/path/to/extra/sys/includes").
        """
        if not self.compiler:
            raise emk.BuildError("No compiler defined!")
        try:
            if cxx:
                self.compiler.compile_cxx(requires[0], produces[0], produces[0] + ".dep", includes, defines, flags)
            else:
                self.compiler.compile_c(requires[0], produces[0], produces[0] + ".dep", includes, defines, flags)
        except:
            utils.rm(produces[0])
            utils.rm(produces[0] + ".dep")
            raise
        