import os
import logging
import shlex
import re
import sys
import traceback

log = logging.getLogger("emk.asm")

utils = emk.module("utils")

fix_path_regex = re.compile(r'[\W]+')

class _GccAssembler(object):
    """
    Assembler class for using gcc as an assembler.
    
    In order for the emk asm module to use an assembler instance, the assembler class must define the following methods:
      load_extra_dependencies
      assemble
      obj_ext
    See the documentation for those functions in this class for more details.
    
    Properties (defaults set based on the path prefix passed to the constructor):
      gcc_path   -- The path of to gcc.
    """
    def __init__(self, path_prefix=""):
        """
        Create a new GccAssembler instance.
        
        Arguments:
          path_prefix -- The prefix to use for the gcc executable. For example, if you had an ARM cross-gcc
                         installed into /cross/arm, you might use 'asm.assembler = asm.GccAssembler("/cross/arm/bin/arm-none-eabi-")'
                         to configure the asm module to use the cross compiler. The default value is "" (ie, use the system gcc).
        """
        self.gcc_path = path_prefix + "gcc"
    
    def load_extra_dependencies(self, path):
        """
        Load extra dependencies for the given object file path. The extra dependencies could be loaded from a generated
        dependency file for that path, or loaded from the emk.scope_cache(path) (or some other mechanism).
        
        Arguments:
          path -- The path of the object file to get dependencies for.
        
        Returns a list of paths (strings) of all the extra dependencies.
        """
        cache = emk.scope_cache(path)
        return cache.get("secondary_deps", [])
    
    def depfile_args(self, dep_file):
        """
        Returns a list of arguments to write secondary dependencies to the given dep_file path.
        """
        return ["-Wp,-MMD,%s" % (dep_file)]
    
    def assemble(self, source, dest, includes, defines, flags):
        dep_file = dest + ".dep"
        args = [self.gcc_path]
        args.extend(self.depfile_args(dep_file))
        args.extend(["-I%s" % (emk.abspath(d)) for d in includes])
        args.extend(["-D%s=%s" % (key, value) for key, value in defines.items()])
        args.extend(utils.flatten(flags))
        args.extend(["-o", dest, "-c", source])
        utils.call(args, print_stderr=False)
        
        try:
            with open(dep_file, "r") as f:
                data = f.read()
                data = data.replace("\\\n", "")
                items = shlex.split(data)
                unique_items = [emk.abspath(item) for item in (set(items[2:]) - set([""]))]
                # call has_changed to set up rule cache for future builds.
                for item in unique_items:
                    emk.current_rule.has_changed(item)
                cache = emk.scope_cache(dest)
                cache["secondary_deps"] = unique_items
        except IOError:
            log.error("Failed to open depfile %s", dep_file)

    def obj_ext(self):
        """
        Get the extension of object files built by this assembler.
        """
        return ".o"

class Module(object):
    """
    emk module for assembling code. Depends on the link module (and utils).
    
    This module defines emk rules during the prebuild stage, to allow autodiscovery of generated source files
    from rules defined before the prebuild stage (ie, in the post_rules() method of other modules). See the
    autodetect and autodetect_from_targets properties for more information about autodiscovery of source files.
    
    This module adds the assembled object files to the link module, which will link them into libraries/executables as desired.
    The object files are added to the link module's 'objects' property (each mapped to the source file that the object file
    was built from), so that the link module can autodetect main() functions from the source (if link.detect_exe == "simple").
    See the link module documentation for details of main() autodetection.
    
    Note that the assembler rules are not built automatically; the link module (or other modules/user code)
    is responsible for marking the object files as autobuild if desired.
    
    Classes:
      GccAssembler -- An assembler class that uses gcc to assemble.
    
    Properties (inherited from parent scope):
      assembler    -- The assembler instance that is used to load dependencies and assemble code.
      include_dirs -- A list of additional include directories.
      defines      -- A dict of <name>: <value> defines; each entry is equivalent to #define <name> <value>.
      flags        -- A list of flags. If you have a 'flag' that is more than one argument, pass it as a tuple.
                      Example: ("-isystem", "/path/to/extra/sys/includes"). Duplicate flags will be removed.
      source_files -- A list of files that should be assembled.
      
      exts         -- The list of file extensions (suffixes) that will be considered as assembly code. The default is [".s", ".S"].
      
      autodetect  -- Whether or not to autodetect files to build from the scope directory. If True, all files that match the exts suffixes
                     will be assembled. Autodetection does not take place until the prebuild stage, so that autodetection of generated
                     code can gather as many targets as possible (see autodetect_from_targets). The default value is True.
      autodetect_from_targets -- Whether or not to autodetect generated code based on rules defined in the current scope.
                                 The default value is True.
      excludes     -- A list of source files to exclude from assembly.
      non_lib_src  -- A list of source files that will not be linked into a library for this directory (passed to the link module).
      non_exe_src  -- A list of source files that will not be linked into an executable, even if they contain a main() function.
      unique_names -- If True, the output object files will be named according to the path from the project directory, to avoid
                      naming conflicts when the build directory is not a relative path. The default value is False.
                      If True, the link module's unique_names property will also be set to True.
      obj_funcs    -- A list of functions that are run for each generated object file path.

      obj_ext      -- The file extension for object files generated by the assembler (eg ".o" for gcc).  This property is
                      read-only as its value is provided by the assembler implementation.
    """
    def __init__(self, scope, parent=None):
        self.GccAssembler = _GccAssembler
        
        self.link = emk.module("link")
        
        if parent:
            self.assembler = parent.assembler
            
            self.include_dirs = list(parent.include_dirs)
            self.defines = parent.defines.copy()
            self.flags = list(parent.flags)
            
            self.source_files = list(parent.source_files)

            self.exts = list(parent.exts)

            self.autodetect = parent.autodetect
            self.autodetect_from_targets = parent.autodetect_from_targets
            self.excludes = list(parent.excludes)
            self.non_lib_src = list(parent.non_lib_src)
            self.non_exe_src = list(parent.non_exe_src)
            
            self.obj_funcs = list(parent.obj_funcs)

            self.unique_names = parent.unique_names
        else:
            self.assembler = self.GccAssembler()
            
            self.include_dirs = []
            self.defines = {}
            self.flags = []
            
            self.source_files = []
            
            self.exts = [".s", ".S"]
        
            self.autodetect = True
            self.autodetect_from_targets = True
            self.excludes = []
            self.non_lib_src = []
            self.non_exe_src = []
            
            self.obj_funcs = []
            
            self.unique_names = False

    @property
    def obj_ext(self):
        return self.assembler.obj_ext()

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
        if self.unique_names and self.link:
            self.link.unique_names = True
    
    def _prebuild(self):
        sources = set()
        
        self._non_exe_src = set(self.non_exe_src)
        self._non_lib_src = set(self.non_lib_src)
        
        if self.autodetect:
            if self.autodetect_from_targets:
                target_files = [t for t in emk.local_targets.keys() if self._matches_exts(t, self.exts)]
                if target_files:
                    log.debug("Detected generated asm files: %s", target_files)
                    self.source_files.extend(target_files)
                    
            files = set(self.source_files)
            files.update([f for f in os.listdir(emk.scope_dir) if os.path.isfile(f)])
            for file_path in files:
                if self._matches_exts(file_path, self.exts):
                    self.source_files.append(file_path)
        
        for f in self.source_files:
            if f in self.excludes:
                continue
            sources.add(f)
        
        args = (self.include_dirs, self.defines, self.flags)
        
        objs = {}
        for src in sources:
            self._add_rule(objs, src, args)
        
        if self.link:
            self.link.objects.update([(os.path.join(emk.build_dir, obj + self.obj_ext), src) for obj, src in objs.items()])
    
    def _add_rule(self, objs, src, args):
        fname = os.path.basename(src)
        n, ext = os.path.splitext(fname)
        if self.unique_names:
            relpath = fix_path_regex.sub('_', os.path.relpath(emk.scope_dir, emk.proj_dir))
            n = relpath + "_" + n
            
        name = n
        c = 1
        while name in objs:
            name = "%s_%s" % (n, c)
            c += 1
        objs[name] = src
        
        if self.link:
            objpath = os.path.join(emk.build_dir, name + self.obj_ext)
            if src in self._non_exe_src:
                self.link.non_exe_objs.append(objpath)
            if src in self._non_lib_src:
                self.link.non_lib_objs.append(objpath)
        
        dest = os.path.join(emk.build_dir, name + self.obj_ext)
        requires = [src]
        extra_deps = None
        if self.assembler:
            extra_deps = self.assembler.load_extra_dependencies(emk.abspath(dest))
        if extra_deps is None:
            requires.append(emk.ALWAYS_BUILD)
        
        emk.rule(self.assemble, dest, requires, *args, cwd_safe=True, ex_safe=True)
        if extra_deps:
            emk.weak_depend(dest, extra_deps)
        for f in self.obj_funcs:
            f(dest)
    
    def assemble(self, produces, requires, includes, defines, flags):
        """
        Rule function to assemble a source file into an object file.
        
        The assembler instance will also produce an <object file>.dep file that contains additional dependencies (ie, header files).
        
        Arguments:
          produces -- The path to the object file that will be produced.
          requires -- The list of dependencies; the source file should be first.
          includes -- A list of additional include directories.
          defines  -- A dict of <name>: <value> entries to be defined (like #define <name> <value>).
          flags    -- A list of flags to pass to the assembler. Compound flags should be in a tuple, eg: ("-isystem", "/path/to/extra/sys/includes").
        """
        if not self.assembler:
            raise emk.BuildError("No assembler defined!")
        try:
            self.assembler.assemble(requires[0], produces[0], includes, defines, flags)
        except:
            utils.rm(produces[0])
            utils.rm(produces[0] + ".dep")
            raise
