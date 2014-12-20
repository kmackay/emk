import os
import sys
import logging
import re
import shutil
import hashlib
import struct

log = logging.getLogger("emk.link")

utils = emk.module("utils")

fix_path_regex = re.compile(r'[\W]+')

class _GccLinker(object):
    """
    Linker class for using gcc/g++ to link.
    
    In order for the emk link module to use a linker instance, the linker class must define the following methods:
      contains_main_function
      create_static_lib
      static_lib_cwd_safe
      shlib_opts
      exe_opts
      do_link
      link_cwd_safe
      strip
    See the documentation for those functions in this class for more details.
    
    Properties (defaults set based on the path prefix passed to the constructor):
      c_path     -- The path of the C linker (eg "gcc").
      cxx_path   -- The path of the C++ linker (eg "g++").
      ar_path    -- The path of the archive tool to create static libs (eg "ar").
      strip_path -- The path of the strip tool to remove unnecessary symbols (eg "strip").
      nm_path    -- The path of the nm tool to read the symbol table from an object file (eg "nm").
      
      main_nm_regex -- The compiled regex to use to search for a main() function in the nm output. The default value
                       looks for a line ending in " T main".
    """
    def __init__(self, path_prefix=""):
        """
        Create a new GccLinker instance.
        
        Arguments:
          path_prefix -- The prefix to use for the various binutils executables (gcc, g++, ar, strip, nm).
                         For example, if you had a 32-bit Linux cross compiler installed into /cross/linux,
                         you might use 'link.linker = link.GccLinker("/cross/linux/bin/i686-pc-linux-gnu-")'
                         to configure the link module to use the cross binutils. The default value is ""
                         (ie, use the system binutils).
        """
        self.name = "gcc"
        self.path_prefix = path_prefix
        self.c_path = path_prefix + "gcc"
        self.cxx_path = path_prefix + "g++"
        self.ar_path = path_prefix + "ar"
        self.strip_path = path_prefix + "strip"
        self.nm_path = path_prefix + "nm"
        
        self.main_nm_regex = re.compile(r'\s+T\s+main\s*$', re.MULTILINE)
    
    def contains_main_function(self, objfile):
        """
        Determine if an object file contains a main() function.
        
        This is used by the link module to autodetect which object files should be linked into executables.
        
        Arguments:
          objfile -- The path to the object file.
        
        Returns True if the object file contains a main() function, False otherwise.
        """
        out, err, code = utils.call(self.nm_path, "-g", objfile, print_call=False)
        if self.main_nm_regex.search(out):
            return True
        return False
        
    def extract_static_lib(self, lib, dest_dir):
        """
        Extract all contents of a static library to the given directory.
        
        Arguments:
          lib      -- The static library to extract from.
          dest_dir -- The directory to extract into.
        """
        log.info("Extracting lib %s", lib)
        utils.call(self.ar_path, "x", lib, print_call=False, cwd=dest_dir)
    
    def add_to_static_lib(self, dest, objs):
        """
        Add some object files to a static library.
        
        If the library does not already exist, it will be created.
        
        Arguments:
          dest -- The path to the static library.
          objs -- A list of paths to object files to add to the static library.
        """
        utils.call(self.ar_path, "r", dest, objs)
    
    def create_static_lib(self, dest, source_objs, other_libs):
        """
        Create a static library (archive) containing the given object files and all object files contained in
        the given other libs (which will be static libraries as well).
        
        Called by the link module to create a static library. If the link module's 'lib_in_lib' property is True,
        the link module will pass in the library dependencies of this library in the 'other_libs' argument. This
        method must include the contents of all the other libraries in the generated static library.
        
        Arguments:
          dest        -- The path of the static library to generate.
          source_objs -- A list of paths to object files to include in the generated static library.
          other_libs  -- A list of paths to other static libraries whose contents should be included in the generated static library.
        """
        objs = list(source_objs)
        hash = hashlib.md5(emk.scope_dir).hexdigest()
        dump_dir = os.path.join(emk.scope_dir, emk.build_dir, "__lib_temp__" + hash)
        utils.mkdirs(dump_dir)

        counter = 0
        for lib in other_libs:
            d = os.path.join(dump_dir, "%d" % (counter))
            shutil.rmtree(d, ignore_errors=True)
            os.mkdir(d)
            
            self.extract_static_lib(lib, d)
            files = [f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)) and f.endswith(self.obj_ext())]
            for file_path in files:
                name, ext = os.path.splitext(file_path)
                new_name = "%s_%s%s" % (name, counter, ext)
                new_path = os.path.join(d, new_name)
                os.rename(os.path.join(d, file_path), new_path)
                objs.append(new_path)
            counter += 1
        
        utils.rm(dest)
        
        # we add files to the archive 32 at a time to avoid command-line length restriction issues.
        left = len(objs)
        start = 0
        while left > 32:
            cur_objs = objs[start:start+32]
            start += 32
            left -= 32
            self.add_to_static_lib(dest, cur_objs)
        cur_objs = objs[start:]
        self.add_to_static_lib(dest, cur_objs)
    
    def static_lib_cwd_safe(self):
        """
        Returns True if creating a static library using the create_static_lib() method is cwd_safe (ie, does not
        use anything that depends on the current working directory); returns False otherwise.
        """
        return True
    
    def shlib_opts(self):
        """
        Returns a list of options that the link module should use when linking a shared library.
        """
        return ["-shared"]
    
    def exe_opts(self):
        """
        Returns a list of options that the link module should use when linking an executable.
        """
        return []
    
    def link_cmd(self, cmd, flags, dest, objs, abs_libs, lib_dirs, libs):
        """
        Set up and call the linker.
        """
        sg = "-Wl,--start-group"
        eg = "-Wl,--end-group"
        call = [cmd] + flags + ["-o", dest] + objs + lib_dirs + [sg] + abs_libs + libs + [eg]
        utils.call(call, print_stderr=False)

    def do_link(self, dest, source_objs, abs_libs, lib_dirs, rel_libs, flags, cxx_mode):
        """
        Link a shared library or executable.
        
        The link module does not order the libraries to be linked in, so this method must ensure that any ordering
        dependencies are solved. The GCC linker uses the '--start-group' and '--end-group' options to make sure
        that library ordering is not an issue; the documentation says that this can be slow but in reality
        it makes very little difference.
        
        Arguments:
          dest        -- The path of the destination file to produce.
          source_objs -- A list of object files to link in.
          abs_libs    -- A list of absolute paths to (static) libraries that should be linked in.
          lib_dirs    -- Additional search paths for relative libraries.
          rel_libs    -- Relative library names to link in.
          flags       -- Additional flags to be passed to the linker.
          cxx_mode    -- If True, then the object files or libraries contain C++ code (so use g++ to link, for example).
        """
        linker = self.c_path
        if cxx_mode:
            linker = self.cxx_path
        
        flat_flags = utils.flatten(flags)
        
        lib_dir_flags = ["-L" + d for d in lib_dirs]
        rel_lib_flags = ["-l" + lib for lib in rel_libs]
        
        self.link_cmd(linker, flat_flags, dest, source_objs, abs_libs, lib_dir_flags, rel_lib_flags)
    
    def link_cwd_safe(self):
        """
        Returns True if linking a shared library or executable using the do_link() method is cwd_safe (ie, does not
        use anything that depends on the current working directory); returns False otherwise.
        """
        return True
        
    def strip(self, path):
        """
        Strip unnecessary symbols from the given shared library / executable. Called by the link module after linking
        if its 'strip' property is True.
        
        Arguments:
          path -- The path of the file to strip.
        """
        utils.call(self.strip_path, "-S", "-x", path)

    def obj_ext(self):
        """
        Get the extension of object files consumed by this linker.
        """
        return ".o"

class _OsxLinker(_GccLinker):
    """
    A linker class for linking on OS X using clang.
    
    Properties:
      lipo_path    -- The path of the 'lipo' executable.
      libtool_path -- The path of the 'libtool' executable.
    """
    def __init__(self, path_prefix=""):
        super(_OsxLinker, self).__init__(path_prefix)
        self.name = "clang"
        self.c_path = path_prefix + "clang"
        self.cxx_path = path_prefix + "clang++"
        self.lipo_path = self.path_prefix + "lipo"
        self.libtool_path = self.path_prefix + "libtool"
        self.main_nm_regex = re.compile(r'\s+T\s+_main\s*$', re.MULTILINE)
    
    def extract_static_lib(self, lib, dest_dir):
        """
        Extract a static library to the given directory, handling fat files properly.
        """
        log.info("Extracting lib %s", lib)
        out, err, ret = utils.call(self.lipo_path, "-info", lib, print_call=False)
        if "is not a fat file" in out:
            utils.call(self.ar_path, "x", lib, print_call=False, cwd=dest_dir)
        else:
            start, mid, rest = out.partition(lib + " are:")
            archs = rest.strip().split(' ')
            objs = set()
            for arch in archs:
                arch_dir = os.path.join(dest_dir, arch)
                os.mkdir(arch_dir)
                utils.call(self.lipo_path, lib, "-thin", arch, "-output", os.path.join(arch_dir, "thin.a"), print_call=False)
                utils.call(self.ar_path, "x", "thin.a", print_call=False, cwd=arch_dir)
                objs.update([f for f in os.listdir(arch_dir) if os.path.isfile(os.path.join(arch_dir, f)) and f.endswith(self.obj_ext())])
            for obj in objs:
                cmd = [self.lipo_path, "-create", "-output", os.path.join(dest_dir, obj)]
                for arch in archs:
                    arch_obj = os.path.join(dest_dir, arch, obj)
                    if os.path.isfile(arch_obj):
                        cmd.append(arch_obj)
                utils.call(cmd, print_call=False)

    def add_to_static_lib(self, dest, objs):
        """
        Add object files to a static library, handling fat files properly.
        """
        cmd = [self.libtool_path, "-static", "-s", "-o", dest, "-"]
        if os.path.isfile(dest):
            cmd.append(dest)
        cmd.extend(objs)
        utils.call(cmd)

    def shlib_opts(self):
        """
        Linker options to build a shared library (dylib) on OS X.
        """
        return ["-dynamiclib"]
    
    def link_cmd(self, cmd, flags, dest, objs, abs_libs, lib_dirs, libs):
        """
        The actual linker call to use on OS X. Note that we don't need '--start-group'/'--end-group' on OS X
        because the OS X linker will search all libraries to resolve symbols, regardless of order.
        """
        call = [cmd] + flags + ["-o", dest] + objs + abs_libs + lib_dirs + libs
        utils.call(call, print_stderr=False)
        
class _MingwGccLinker(_GccLinker):
    """
    A subclass of GccLinker for linking on Windows.
    """
    def __init__(self, path_prefix=""):
        super(_MingwGccLinker, self).__init__(path_prefix)
        self.main_nm_regex = re.compile(r'\s+T\s+_?((t|w)?main|WinMain(@[0-9]+)?)\s*$', re.MULTILINE)

class _MsvcLinker(object):
    """
    Linker class for using Visual Studio's command line tools to link.
    """
    @staticmethod
    def vs_env(path_prefix, env_script, target_arch):
        """
        Try to locate and load the environment of Visual Studio.

        Arguments:
          path_prefix - The prefix to use for the vcvarsall.bat file. The default value is derived from the VS*COMNTOOLS environment variable.
        """
        if path_prefix is None:
            for v in [120, 110, 100, 90, 80, 71, 70]:
                try:
                    path_prefix = os.path.join(os.environ["VS%uCOMNTOOLS" % v], "..", "..", "VC")
                except KeyError:
                    continue
                else:
                    break
        if path_prefix is None:
            raise BuildError("No installed version of Visual Studio could be found")

        try:
            arch = os.environ["PROCESSOR_ARCHITECTURE"].lower()
            if arch != "amd64":
                arch = os.environ["PROCESSOR_ARCHITEW6432"].lower()
        except KeyError:
            arch = "x86"
        
        if target_arch is not None and target_arch != arch:
            arch = arch + "_" + target_arch

        vcvars = os.path.join(path_prefix, env_script)
        env = utils.get_environment_from_batch_command([vcvars, arch])
        if "VCINSTALLDIR" not in env:
            env = utils.get_environment_from_batch_command(vcvars)
        return env

    def __init__(self, path_prefix=None, env_script="vcvarsall.bat", target_arch=None):
        """
        Create a new MsvcLinker instance.
        
        Arguments:
          path_prefix -- The prefix to use for the vcvarsall.bat file. The default value is derived from the VS*COMNTOOLS environment variable.

        Properties:
          dumpbin_exe -- The absolute path to the dumpbin executable.
          lib_exe     -- The absolute path to the lib executable.
          link_exe    -- The absolute path to the link executable.

          main_dumpbin_regex -- The compiled regex to use to search for a main() function in the dumpbin output.
        """
        self.name = "msvc"
        self._env = _MsvcLinker.vs_env(path_prefix, env_script, target_arch)

        self.dumpbin_exe = "dumpbin.exe"
        self.lib_exe = "lib.exe"
        self.link_exe = "link.exe"

        self.main_dumpbin_regex = re.compile(r'External\s*\|\s*_?(w?main|WinMain)\b')
    
    def contains_main_function(self, objfile):
        """
        Determine if an object file contains a main() function.
        
        This is used by the link module to autodetect which object files should be linked into executables.
        
        Arguments:
          objfile -- The path to the object file.
        
        Returns True if the object file contains a main() function, False otherwise.
        """
        out, err, code = utils.call(self.dumpbin_exe, "/SYMBOLS", objfile, env=self._env, print_call=False)
        return (self.main_dumpbin_regex.search(out) is not None)

    def add_to_static_lib(self, dest, files):
        """
        Add some files (libraries or objects) to a static library.
        
        If the library does not already exist, it will be created.
        
        Arguments:
          dest  -- The path to the static library.
          files -- A list of paths to files to add to the static library.
        """
        utils.call(self.lib_exe, "/NOLOGO", '/OUT:%s' % dest, files, env=self._env, print_stdout=True, print_stderr=False, error_stream="stdout")

    def create_static_lib(self, dest, source_objs, other_libs):
        """
        Create a static library (archive) containing the given object files and all object files contained in
        the given other libs (which will be static libraries as well).
        
        Called by the link module to create a static library. If the link module's 'lib_in_lib' property is True,
        the link module will pass in the library dependencies of this library in the 'other_libs' argument. This
        method must include the contents of all the other libraries in the generated static library.
        
        Arguments:
          dest        -- The path of the static library to generate.
          source_objs -- A list of paths to object files to include in the generated static library.
          other_libs  -- A list of paths to other static libraries whose contents should be included in the generated static library.
        """
        files = list(source_objs) + list(other_libs)

        # we add files to the archive 32 at a time to avoid command-line length restriction issues.
        left = len(files)
        start = 0
        while left > 32:
            cur_files = files[start:start+32]
            if start != 0:
                cur_files.append(dest)
            start += 32
            left -= 32
            self.add_to_static_lib(dest, cur_files)
        cur_files = files[start:]
        if(start != 0):
            cur_files.append(dest)
        self.add_to_static_lib(dest, cur_files)
    
    def static_lib_cwd_safe(self):
        """
        Returns True if creating a static library using the create_static_lib() method is cwd_safe (ie, does not
        use anything that depends on the current working directory); returns False otherwise.
        """
        return True
    
    def shlib_opts(self):
        return ["/DLL"]
    
    def exe_opts(self):
        return []

    def do_link(self, dest, source_objs, abs_libs, lib_dirs, rel_libs, flags, cxx_mode):
        """
        Link a shared library or executable.
        
        Arguments:
          dest        -- The path of the destination file to produce.
          source_objs -- A list of object files to link in.
          abs_libs    -- A list of absolute paths to (static) libraries that should be linked in.
          lib_dirs    -- Additional search paths for relative libraries.
          rel_libs    -- Relative library names to link in.
          flags       -- Additional flags to be passed to the linker.
          cxx_mode    -- If True, then the object files or libraries contain C++ code.
        """
        flat_flags = utils.flatten(flags)
        lib_dir_flags = ['/LIBPATH:%s' % d for d in lib_dirs]
        rel_libs = [lib + ".lib" for lib in rel_libs]
        
        resp_path = "%s.tmp.resp" % (dest)
        with open(resp_path, "wb") as f:
            args = list(utils.flatten([source_objs, abs_libs, lib_dir_flags, rel_libs]))
            f.write(" ".join(args))
            f.close();
        
        if not os.path.isfile(resp_path):
            raise emk.BuildError("Failed to create response file for link.exe")
        
        log.info("File exists here...")

        utils.call(self.link_exe, "/NOLOGO", flat_flags, '/OUT:%s' % dest, "@%s" % resp_path,
            env=self._env, print_stdout=False, print_stderr=False, error_stream="both")
    
    def link_cwd_safe(self):
        """
        Returns True if linking a shared library or executable using the do_link() method is cwd_safe (ie, does not
        use anything that depends on the current working directory); returns False otherwise.
        """
        return True
        
    def strip(self, path):
        """
        Strip unnecessary symbols from the given shared library / executable. Called by the link module after linking
        if its 'strip' property is True.
        
        Arguments:
          path -- The path of the file to strip.
        """
        # Visual Studio puts this sort of information in a separate PDB file already, so there is no need to strip it out of the binary
        pass

    def obj_ext(self):
        """
        Get the extension of object files consumed by this linker.
        """
        return ".obj"

link_cache = {}
need_depdirs = {}

class Module(object):
    """
    emk module for linking compiled code (ie, .o files) into libraries/executables. Depends on the utils module.
    
    This module defines emk rules during the second prebuild stage, so that the c module's prebuild step is executed
    before any link rules are defined. Future modules that create linkable files (eg assembler?) could work in a similar manner
    to the c module, adding to the link.objects dict during the prebuild stage.
    
    By default, the link module will autodetect object files that contain a main() function; those object files will
    each be linked into an executable. The other files (that do not contain main()) are linked into a static library,
    which is used when linking the executables. You may specify dependencies on other directories managed by emk
    (in the same project or a different project); the static libraries from those directories will be linked in as well.
    
    You can also build a shared library instead of or in addition to the static library. Note that on many platforms,
    the object files linked into a shared library must be compiled as position independent code (eg '-fPIC' or '-fpic' with gcc).
    You must configure the necessary flags in the c module for this to work.
    
    By default, the main() function detection occurs after the object files have been generated. This is to allow
    inspection of the symbols in the object files to see if they export a main() function - this is much faster
    and more exact than trying to parse the source code. One side effect of this is that the link module's
    rules are (by default) executed in the second build phase. If this is undesirable for some reason, you can
    configure the link module to do "simple" main() detection (by parsing the source code) by setting the
    'detect_exe' property to "simple". If this is set (or if main() detection is disabled entirely), the link rules
    will be defined and executed in the first build phase.
    
    The link module will always define rules creating the "link.__static_lib__" and "link.__exes__" targets.
    "link.__static_lib__" depends on the static library being generated (or nothing, if there is no static library to generate).
    "link.__exes__" depends on all executables being linked. If a shared library is being created, the link module will
    define a rule for "link.__shared_lib__" that depends on the shared library.
    
    If the 'detect_exe' property is set to "exact", then the link module defines an autobuild rule for "link.__interim__"
    which depends on all object files. This will cause all object files to be built if required in the first build phase,
    so that main() detection can occur in postbuild.
    
    For an object file <name>.o that is being linked into an executable, the generated executable path will be <build dir>/<name><link.exe_ext>
    (note that the 'exe_ext' property is "" by default).
    
    Classes:
      GccLinker      -- A linker class that uses gcc/g++ to link, and uses ar to create static libraries.
      OsxLinker      -- A linker class for linking using gcc/g++ on OS X. Uses libtool to create static libraries.
      MingwGccLinker -- A linker class that uses gcc/g++ to link on Windows, and uses ar to create static libraries.
      MsvcLinker     -- A linker class that uses MSVC's link.exe to link, and lib.exe to create static libraries.
    
    Properties (inherited from parent scope):
      comments_regex      -- The regex to use to match (and ignore) comments when using "simple" main() detection.
      main_function_regex -- The regex to use to detect a main() function when using "simple" main() detection.
      
      linker         -- The linker instance used to link executables / shared libraries, and to create static libraries.
                        This is set to link.GccLinker() by default on Linux, and link.OsxLinker() by default on OS X.
      shared_lib_ext -- The extension to use for shared libraries. The default is ".so" on Linux, and ".dylib" on OS X.
      static_lib_ext -- The extension for static libraries. Set to ".a" by default.
      exe_ext        -- The extension to use for exectuables. Set to "" (empty string) by default.
      lib_prefix     -- The prefix to use for static/shared libraries. Set to "lib" by default.
      obj_ext        -- The file extension for object files processed by the linker (eg ".o" for gcc or ".obj" for MSVC).  This property is
                        read-only as its value is provided by the linker implementation.
      
      shared_libname -- The name to use for the generated shared library (if any). If set to None, the library name will
                        be <lib_prefix><current directory name><shared_lib_ext>. The default value is None.
      static_libname -- The name to use for the generated lib_in_lib static library (if any). If set to None, the library name will
                        be <lib_prefix><current directory name>_all<static_lib_ext>. The default value is None.
                        Note that the regular static library (not lib_in_lib) is always named <lib_prefix><current directory name><static_lib_ext>.
      
      detect_exe      -- The method to use for executable detection (ie, if an object file exports a main() function).
                         If set to "exact", the link module uses the linker instance's 'contains_main_function' method
                         to determine if each object file contains a main() function. If set to "simple", the link module
                         will use the comments_regex and main_function_regex to determine if the source file that generated
                         each object file contains a main() function (note that this only applies to object files for which
                         the source is known, ie the contents of the 'objects' dict). If set to False/None, then no automatic
                         detection of executables will be performed. The default value is "exact".
      link_cxx        -- If True, the link module will tell the linker instance to link C++ code. If False, the link will be done
                         for C code. The default value is False, but may be set to True by the c module if any C++ source files
                         are detected. Note that C++ mode will be used for linking if any of the library dependencies (from the
                         'depdirs' and 'projdirs' properties) contain C++ code.
      make_static_lib -- Whether or not to create a static library containing the non-executable object files.
                         The default value is True.
      make_shared_lib -- Whether or not to create a shared library containing the non-executable files (linked with all library dependencies).
                         The default value is False.
      strip           -- Whether or not to strip the resulting shared library and/or executables. The default value is False.
      lib_in_lib      -- If True (and a static library is being created), the link module will create an additional static library
                         named <lib_prefix><current directory name>_all<static_lib_ext> (or <static_libname>, if set) which
                         contains the local library contents as well as the contents of all library dependencies from 'local_static_libs',
                         and transitively all 'static_libs', 'depdirs', and 'projdirs' libraries - ie the link module will recursively
                         gather all the static library dependencies from all the dependency directories. Useful for generating a
                         single static library for release that contains all of its dependencies.
      unique_names    -- If True, the output libraries/executables will be named according to the path from the project directory, to avoid
                         naming conflicts when the build directory is not a relative path. The default value is False.
      
      exe_objs     -- A list of object files to link into executables (without checking whether they contain a main() function).
      non_exe_objs -- A list of object files that should not be linked into an executable, even if they contain a main() function.
      objects      -- A dict mapping <object file>: <source file>. This allows the link module to determine which source file
                      was compiled to each object file when "simple" main detection is being used. Filled in by the c module.
      obj_nosrc    -- A list of object files for which the source file is not known.
      non_lib_objs -- A list of object files which should not be linked into a library (static or shared).
      
      depdirs      -- A list of directories that the object files in this directory depend on. The link module will instruct emk
                      to recurse into these directories. When linking, the flags, static libs, and syslibs from these directory
                      dependencies will be included in the link (including any from depdirs of the depdirs, and so on - the flags
                      and libs are gathered transitively). It is acceptable to have circular dependencies in the depdirs.
      projdirs     -- A list of dependency directories (like depdirs) that are resolved relative to the project directory.
      
      static_libs        -- A list of paths to static libraries to link in (transitively included by links that depend on this directory).
                            Relative paths will be resolved relative to the current scope.
      local_static_libs  -- A list of paths to static libraries to link in; not transitively included.
                            Relative paths will be resolved relative to the current scope.
      syslibs            -- A list of library names to link in (like '-l<name>.'). Transitively included by links that depend on this directory.
      local_syslibs      -- A list of library names to link in; not transitively included.
      syslib_paths       -- A list of directories to search for named libraries (ie syslibs). Transitively included by links that depend on this directory.
                            Relative paths will be resolved relative to the current scope.
      local_syslib_paths -- A list of directories to search for named libraries; not transitively included.
                            Relative paths will be resolved relative to the current scope.
      
      flags          -- A list of additional flags to pass to the linker (transitively included by links that depend on this directory).
      local_flags    -- A list of additional flags to pass to the linker; not transitively included.
      libflags       -- A list of additional flags to pass to the linker when linking a shared library. Transitively included by links
                        that depend on this directory.
      local_libflags -- A list of additional flags to pass to the linker when linking a shared library; not transitively included.
      exeflags       -- A list of additional flags to pass to the linker when linking an executable. Transitively included by links
                        that depend on this directory.
      local_exeflags -- A list of additional flags to pass to the linker when linking an executable; not transitively included.
      
      There are also c-specific and c++-specific versions of the above flags, which are accessed through c.flags (, c.local_flags, ...) and
      cxx.flags (, cxx.local_flags, ...) respectively.
      
      exe_funcs        -- A list of functions that are run for each generated executable path.
      static_lib_funcs -- A list of functions that are run for each generated static library path.
      shared_lib_funcs -- A list of functions that are run for each generated shared library path.
    """
    def __init__(self, scope, parent=None):
        self.GccLinker = _GccLinker
        self.OsxLinker = _OsxLinker
        self.MingwGccLinker = _MingwGccLinker
        self.MsvcLinker = _MsvcLinker
        
        self._all_depdirs = set()
        self._depended_by = set()
        self._all_static_libs = set()
        self._static_libpath = None
        self._syslib_paths = set()
        self._local_syslib_paths = set()
        self._static_libs = set()
        self._local_static_libs = set()
        
        self.c = emk.Container()
        self.cxx = emk.Container()
        
        if parent:
            self.comments_regex = parent.comments_regex
            self.main_function_regex = parent.main_function_regex
            
            self.linker = parent.linker
            
            self.shared_lib_ext = parent.shared_lib_ext
            self.static_lib_ext = parent.static_lib_ext
            self.exe_ext = parent.exe_ext
            self.lib_prefix = parent.lib_prefix
            
            self.shared_libname = parent.shared_libname
            self.static_libname = parent.static_libname
            
            self.detect_exe = parent.detect_exe
            self.link_cxx = parent.link_cxx
            self.make_static_lib = parent.make_static_lib
            self.make_shared_lib = parent.make_shared_lib
            self.strip = parent.strip
            self.lib_in_lib = parent.lib_in_lib
            self.unique_names = parent.unique_names
            
            self.exe_objs = list(parent.exe_objs)
            self.non_exe_objs = list(parent.non_exe_objs)
            self.objects = parent.objects.copy()
            self.obj_nosrc = list(parent.obj_nosrc)
            self.non_lib_objs = list(parent.non_lib_objs)
            
            self.depdirs = list(parent.depdirs)
            self.projdirs = list(parent.projdirs)
            
            self.static_libs = list(parent.static_libs)
            self.local_static_libs = list(parent.local_static_libs)
            self.syslibs = list(parent.syslibs)
            self.local_syslibs = list(parent.local_syslibs)
            self.syslib_paths = list(parent.syslib_paths)
            self.local_syslib_paths = list(parent.local_syslib_paths)
            
            self.flags = list(parent.flags)
            self.local_flags = list(parent.local_flags)
            self.libflags = list(parent.libflags)
            self.local_libflags = list(parent.local_libflags)
            self.exeflags = list(parent.exeflags)
            self.local_exeflags = list(parent.local_exeflags)
            
            self.c.flags = list(parent.c.flags)
            self.c.local_flags = list(parent.c.local_flags)
            self.c.libflags = list(parent.c.libflags)
            self.c.local_libflags = list(parent.c.local_libflags)
            self.c.exeflags = list(parent.c.exeflags)
            self.c.local_exeflags = list(parent.c.local_exeflags)
            
            self.cxx.flags = list(parent.cxx.flags)
            self.cxx.local_flags = list(parent.cxx.local_flags)
            self.cxx.libflags = list(parent.cxx.libflags)
            self.cxx.local_libflags = list(parent.cxx.local_libflags)
            self.cxx.exeflags = list(parent.cxx.exeflags)
            self.cxx.local_exeflags = list(parent.cxx.local_exeflags)
            
            self.exe_funcs = list(parent.exe_funcs)
            self.static_lib_funcs = list(parent.static_lib_funcs)
            self.shared_lib_funcs = list(parent.shared_lib_funcs)
        else:
            self.comments_regex = re.compile(r'(/\*.*?\*/)|(//.*?$)', re.MULTILINE | re.DOTALL)
            self.main_function_regex = re.compile(r'int\s+main\s*\(')
            self.static_lib_ext = ".a"
            self.exe_ext = ""
            self.lib_prefix = "lib"
            
            if sys.platform == "darwin":
                self.linker = self.OsxLinker()
                self.shared_lib_ext = ".dylib"
            elif sys.platform == "win32":
                self.linker = self.MingwGccLinker()
                self.shared_lib_ext = ".dll"
                self.static_lib_ext = ".lib"
                self.exe_ext = ".exe"
                self.lib_prefix = ""
                self.main_function_regex = re.compile(r'(int\s+_?(t|w)?main\s*\()|(WinMain\s*\()')
            else:
                self.linker = self.GccLinker()
                self.shared_lib_ext = ".so"
            
            self.shared_libname = None
            self.static_libname = None
            
            self.detect_exe = "exact" # could also be "simple" or False
            self.link_cxx = False
            self.make_static_lib = True
            self.make_shared_lib = False
            self.strip = False
            self.lib_in_lib = False
            self.unique_names = False
            
            self.exe_objs = []
            self.non_exe_objs = []
            self.objects = {}
            self.obj_nosrc = []
            self.non_lib_objs = []
            
            self.depdirs = []
            self.projdirs = []
            
            self.static_libs = []
            self.local_static_libs = []
            self.syslibs = []
            self.local_syslibs = []
            self.syslib_paths = []
            self.local_syslib_paths = []
            
            self.flags = []
            self.local_flags = []
            self.libflags = []
            self.local_libflags = []
            self.exeflags = []
            self.local_exeflags = []
            
            self.c.flags = []
            self.c.local_flags = []
            self.c.libflags = []
            self.c.local_libflags = []
            self.c.exeflags = []
            self.c.local_exeflags = []
            
            self.cxx.flags = []
            self.cxx.local_flags = []
            self.cxx.libflags = []
            self.cxx.local_libflags = []
            self.cxx.exeflags = []
            self.cxx.local_exeflags = []
            
            self.exe_funcs = []
            self.static_lib_funcs = []
            self.shared_lib_funcs = []
    
    @property
    def obj_ext(self):
        return self.linker.obj_ext()

    def new_scope(self, scope):
        return Module(scope, parent=self)
    
    def post_rules(self):
        if not emk.cleaning:
            emk.do_prebuild(self._prebuild)
    
    def _get_needed_by(self, d, result):
        global link_cache
        if(d == emk.scope_dir):
            return
        result.add(d)
        for sub in link_cache[d]._depended_by:
            if not sub in result:
                self._get_needed_by(sub, result)
    
    def _prebuild(self):
        global link_cache
        global need_depdirs
        
        self._syslib_paths = set([emk.abspath(d) for d in self.syslib_paths])
        self._local_syslib_paths = set([emk.abspath(d) for d in self.local_syslib_paths])
        self._static_libs = set([emk.abspath(lib) for lib in self.static_libs])
        self._local_static_libs = set([emk.abspath(lib) for lib in self.local_static_libs])
        
        for d in self.projdirs:
            self.depdirs.append(os.path.join(emk.proj_dir, d))
        self.projdirs = []

        self._all_static_libs.update(self._static_libs)
        
        for d in set(self.depdirs):
            abspath = emk.abspath(d)
            self._all_depdirs.add(abspath)
            if abspath in link_cache:
                dep_link = link_cache[abspath]
                self._all_depdirs.update(dep_link._all_depdirs)
                self._all_static_libs.update(dep_link._all_static_libs)
                dep_link._depended_by.add(emk.scope_dir)
            elif abspath in need_depdirs:
                need_depdirs[abspath].add(emk.scope_dir)
            else:
                need_depdirs[abspath] = set([emk.scope_dir])
            emk.recurse(d)
        
        needed_by = set()
        if emk.scope_dir in need_depdirs:
            for d in need_depdirs[emk.scope_dir]:
                self._depended_by.add(d)
                self._get_needed_by(d, needed_by)

        lib_deps = [os.path.join(d, "link.__static_lib__") for d in self._all_depdirs]
        for d in needed_by:
            cached = link_cache[d]
            cached._all_depdirs.update(self._all_depdirs)
            cached._all_static_libs.update(self._all_static_libs)
            emk.depend(os.path.join(d, "link.__exe_deps__"), lib_deps)
            emk.depend(os.path.join(d, "link.__exe_deps__"), self._all_static_libs)
        
        if self.detect_exe == "exact":
            emk.require_rule("link.__static_lib__", "link.__lib_in_lib__", "link.__shared_lib__", "link.__exe_deps__", "link.__exes__")
            dirname = os.path.basename(emk.scope_dir)
            if self.unique_names:
                dirname = fix_path_regex.sub('_', os.path.relpath(emk.scope_dir, emk.proj_dir))
                
            if self.make_static_lib:
                if self.lib_in_lib:
                    all_libname = libname = self.lib_prefix + dirname + "_all" + self.static_lib_ext
                    only_libname = self.lib_prefix + dirname + self.static_lib_ext
                    if self.static_libname:
                        all_libname = self.static_libname
                        only_libname = "only_" + self.static_libname
                    emk.require_rule(os.path.join(emk.build_dir, all_libname))
                    emk.require_rule(os.path.join(emk.build_dir, only_libname))
                else:
                    libname = self.lib_prefix + dirname + self.static_lib_ext
                    if self.static_libname:
                        libname = self.static_libname
                    emk.require_rule(os.path.join(emk.build_dir, libname))
            if self.make_shared_lib:
                libname = self.lib_prefix + dirname + self.shared_lib_ext
                if self.shared_libname:
                    libname = self.shared_libname
                emk.require_rule(os.path.join(emk.build_dir, libname))
                
            emk.do_prebuild(self._create_interim_rule)
        else:
            emk.do_prebuild(self._create_rules)
        
        link_cache[emk.scope_dir] = self
    
    def _create_interim_rule(self):
        all_objs = set(self.obj_nosrc) | set([obj for obj, src in self.objects.items()]) | set(self.exe_objs)
        all_objs.add(emk.ALWAYS_BUILD)
        emk.rule(self._interim_rule, "link.__interim__", all_objs)
        emk.autobuild("link.__interim__")
        
    def _interim_rule(self, produces, requires):
        self._create_rules()
        emk.mark_virtual(produces)

    def _simple_detect_exe(self, sourcefile):
        with open(sourcefile) as f:
            data = f.read()
            text = self.comments_regex.sub('', data)
            if self.main_function_regex.search(text):
                return True
            return False

    def _create_rules(self):
        global link_cache
        
        exe_objs = set(self.exe_objs)
        non_exe_objs = set(self.non_exe_objs)
        obj_nosrc = set(self.obj_nosrc)
        all_objs = obj_nosrc | set([obj for obj, src in self.objects.items()])
        
        if not self.detect_exe:
            pass
        elif self.detect_exe is True or self.detect_exe.lower() == "simple":
            for obj, src in self.objects.items():
                if (not obj in exe_objs) and (not obj in non_exe_objs) and self._simple_detect_exe(src):
                    exe_objs.add(obj)
        elif self.detect_exe.lower() == "exact":
            for obj, src in self.objects.items():
                if (not obj in exe_objs) and (not obj in non_exe_objs) and self.linker.contains_main_function(obj):
                    exe_objs.add(obj)
            for obj in obj_nosrc:
                if (not obj in exe_objs) and (not obj in non_exe_objs) and self.linker.contains_main_function(obj):
                    exe_objs.add(obj)
        
        lib_objs = all_objs - exe_objs
        lib_objs -= set(self.non_lib_objs)
        
        utils.mark_virtual_rule(["link.__exe_deps__"], ["link.__static_lib__"])
        
        lib_deps = [os.path.join(d, "link.__static_lib__") for d in self._all_depdirs]
        emk.depend("link.__exe_deps__", lib_deps)
        emk.depend("link.__exe_deps__", self._all_static_libs)
        emk.depend("link.__exe_deps__", self._local_static_libs)
        
        dirname = os.path.basename(emk.scope_dir)
        if self.unique_names:
            dirname = fix_path_regex.sub('_', os.path.relpath(emk.scope_dir, emk.proj_dir))
        making_static_lib = False
        if lib_objs:
            if self.make_static_lib:
                making_static_lib = True
                libname = self.lib_prefix + dirname + self.static_lib_ext
                if self.static_libname:
                    if self.lib_in_lib:
                        libname = "only_" + self.static_libname
                    else:
                        libname = self.static_libname
                libpath = os.path.join(emk.build_dir, libname)
                self._static_libpath = libpath
                emk.rule(self._create_static_lib, libpath, lib_objs, False, cwd_safe=self.linker.static_lib_cwd_safe(), ex_safe=True)
                emk.alias(libpath, "link.__static_lib__")
                emk.autobuild(libpath)
                for f in self.static_lib_funcs:
                    f(libpath)
            if self.make_shared_lib:
                libname = self.lib_prefix + dirname + self.shared_lib_ext
                if self.shared_libname:
                    libname = self.shared_libname
                libpath = os.path.join(emk.build_dir, libname)
                emk.rule(self._create_shared_lib, libpath, ["link.__exe_deps__"] + list(lib_objs), cwd_safe=self.linker.link_cwd_safe(), ex_safe=True)
                emk.autobuild(libpath)
                emk.alias(libpath, "link.__shared_lib__")
                for f in self.shared_lib_funcs:
                    f(libpath)
        if not making_static_lib:
            utils.mark_virtual_rule(["link.__static_lib__"], [])
        
        if lib_objs and not (self.make_static_lib or self.make_shared_lib):
            utils.mark_virtual_rule(["link.__force_compile__"], lib_objs)
            emk.autobuild("link.__force_compile__")
        
        if self.make_static_lib and self.lib_in_lib:
            libname = self.lib_prefix + dirname + "_all" + self.static_lib_ext
            if self.static_libname:
                libname = self.static_libname
            libpath = os.path.join(emk.build_dir, libname)
            emk.rule(self._create_static_lib, libpath, ["link.__static_lib__", "link.__exe_deps__"], True, \
                cwd_safe=self.linker.static_lib_cwd_safe(), ex_safe=True)
            emk.alias(libpath, "link.__lib_in_lib__")
            emk.autobuild(libpath)
            for f in self.static_lib_funcs:
                f(libpath)
        
        exe_targets = []
        exe_names = set()
        for obj in exe_objs:
            basename = os.path.basename(obj)
            n, ext = os.path.splitext(basename)
            if self.unique_names:
                relpath = fix_path_regex.sub('_', os.path.relpath(emk.scope_dir, emk.proj_dir))
                n = relpath + "_" + n
            
            name = n
            c = 1
            while name in exe_names:
                name = "%s_%s" % (n, c)
                c += 1
            exe_names.add(name)
            name = name + self.exe_ext
            
            path = os.path.join(emk.build_dir, name)
            emk.rule(self._create_exe, path, [obj, "link.__exe_deps__"], cwd_safe=self.linker.link_cwd_safe(), ex_safe=True)
            emk.alias(path, name)
            exe_targets.append(path)
            for f in self.exe_funcs:
                f(path)
            
        utils.mark_virtual_rule(["link.__exes__"], exe_targets)
        emk.autobuild("link.__exes__")
    
    def _create_static_lib(self, produces, requires, lib_in_lib):
        global link_cache
        
        objs = []
        other_libs = set()
        if lib_in_lib:
            if self._static_libpath:
                other_libs.add(emk.abspath(self._static_libpath))
            other_libs |= self._local_static_libs
            other_libs |= self._static_libs
            for d in self._all_depdirs:
                cache = link_cache[d]
                if cache._static_libpath:
                    other_libs.add(os.path.join(d, cache._static_libpath))
                other_libs |= cache._static_libs
        else:
            objs = requires
        
        try:
            self.linker.create_static_lib(produces[0], objs, other_libs)
        except:
            utils.rm(produces[0])
            raise
    
    def _create_shared_lib(self, produces, requires):
        global link_cache
        
        flags = self.linker.shlib_opts() + self.local_flags + self.flags + self.local_libflags + self.libflags
        c_flags = self.c.local_flags + self.c.flags + self.c.local_libflags + self.c.libflags
        cxx_flags = self.cxx.local_flags + self.cxx.flags + self.cxx.local_libflags + self.cxx.libflags

        abs_libs = self._local_static_libs | self._static_libs
        syslibs = set(self.local_syslibs) | set(self.syslibs)
        lib_paths = self._syslib_paths | self._local_syslib_paths
        link_cxx = self.link_cxx
        
        for d in self._all_depdirs:
            cache = link_cache[d]
            flags += cache.flags
            flags += cache.libflags
            c_flags += cache.c.flags
            c_flags += cache.c.libflags
            cxx_flags += cache.cxx.flags
            cxx_flags += cache.cxx.libflags
            
            if cache._static_libpath:
                abs_libs.add(os.path.join(d, cache._static_libpath))
            abs_libs |= cache._static_libs
            syslibs |= set(cache.syslibs)
            lib_paths |= cache._syslib_paths
            link_cxx = link_cxx or cache.link_cxx
        
        if link_cxx:
            flags += cxx_flags
        else:
            flags += c_flags
        
        try:
            self.linker.do_link(produces[0], [o for o in requires if o.endswith(self.obj_ext)], list(abs_libs), \
                lib_paths, syslibs, utils.unique_list(flags), cxx_mode=link_cxx)
            if self.strip:
                self.linker.strip(produces[0])
        except:
            utils.rm(produces[0])
            raise
    
    def _create_exe(self, produces, requires):
        global link_cache
        
        flags = self.linker.exe_opts() + self.local_flags + self.flags + self.local_exeflags + self.exeflags
        c_flags = self.c.local_flags + self.c.flags + self.c.local_exeflags + self.c.exeflags
        cxx_flags = self.cxx.local_flags + self.cxx.flags + self.cxx.local_exeflags + self.cxx.exeflags

        abs_libs = self._local_static_libs | self._static_libs
        if self._static_libpath:
            abs_libs.add(emk.abspath(self._static_libpath))
        syslibs = set(self.local_syslibs) | set(self.syslibs)
        lib_paths = self._syslib_paths | self._local_syslib_paths
        link_cxx = self.link_cxx
        
        for d in self._all_depdirs:
            cache = link_cache[d]
            flags += cache.flags
            flags += cache.exeflags
            c_flags += cache.c.flags
            c_flags += cache.c.exeflags
            cxx_flags += cache.cxx.flags
            cxx_flags += cache.cxx.exeflags
            
            if cache._static_libpath:
                abs_libs.add(os.path.join(d, cache._static_libpath))
            abs_libs |= cache._static_libs
            syslibs |= set(cache.syslibs)
            lib_paths |= cache._syslib_paths
            link_cxx = link_cxx or cache.link_cxx
        
        if link_cxx:
            flags += cxx_flags
        else:
            flags += c_flags
        
        try:
            self.linker.do_link(produces[0], [o for o in requires if o.endswith(self.obj_ext)], list(abs_libs), \
                lib_paths, syslibs, utils.unique_list(flags), cxx_mode=link_cxx)
            if self.strip:
                self.linker.strip(produces[0])
        except:
            utils.rm(produces[0])
            raise
