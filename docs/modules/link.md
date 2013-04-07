Link Module
===========

The link module is an emk module for linking compiled code (ie, .o files) into libraries/executables. It can support various
linkers by setting the `linker` property to a different instance. The default linker instance uses GCC and associated tools for linking.
You can create a new instance of `link.GccLinker` to use a different version of gcc (eg for cross-compilation).
To support other linkers, you would need to implement a linker class (see "Linker Support" below).

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

Using the `depdirs` or `projdirs` properties, you can specify dependencies on other directories that are built using emk.
When linking an executable or shared library, emk will gather all (non-local) linker flags and libraries from all depdirs and projdirs
(transitively) to pass to the linker. This allows you to specify flags with the code that requires them, instead of where
that code is being linked in.

By default, the main() function detection occurs after the object files have been generated. This is to allow
inspection of the symbols in the object files to see if they export a main() function - this is much faster
and more exact than trying to parse the source code. One side effect of this is that the link module's
rules are (by default) executed in the second build phase. If this is undesirable for some reason, you can
configure the link module to do "simple" main() detection (by parsing the source code) by setting the
`detect_exe` property to "simple". If this is set (or if main() detection is disabled entirely), the link rules
will be defined and executed in the first build phase.

The link module will always define rules creating the "link.__static_lib__" and "link.__exes__" targets.
"link.__static_lib__" depends on the static library being generated (or nothing, if there is no static library to generate).
"link.__exes__" depends on all executables being linked. If a shared library is being created, the link module will
define a rule for "link.__shared_lib__" that depends on the shared library.

If the 'detect_exe' property is set to "exact", then the link module defines an autobuild rule for "link.__interim__"
which depends on all object files. This will cause all object files to be built if required in the first build phase,
so that main() detection can occur in postbuild.

For an object file &lt;name>.o that is being linked into an executable, the generated executable path will be &lt;build dir>/&lt;name>&lt;link.exe_ext>
(note that the 'exe_ext' property is "" by default).

Classes
-------
#### **GccLinker**: A linker class that uses gcc/g++ to link, and uses ar to create static libraries.

Properties (defaults set based on the path prefix passed to the constructor):
 * **c_path**: The path of the C linker (eg "gcc").
 * **cxx_path**: The path of the C++ linker (eg "g++").
 * **ar_path**: The path of the archive tool to create static libs (eg "ar").
 * **strip_path**: The path of the strip tool to remove unnecessary symbols (eg "strip").
 * **nm_path**: The path of the nm tool to read the symbol table from an object file (eg "nm").
 * **main_nm_regex**: The compiled regex to use to search for a main() function in the nm output. The default value
                      looks for a line ending in " T main".
 
#### **OsxGccLinker**: A linker class for linking using gcc/g++ on OS X; inherits from GccLinker. Uses libtool to create static libraries for multi-arch support.

Properties (defaults set based on the path prefix passed to the constructor):
 * **lipo_path**: The path of the 'lipo' executable.
 * **libtool_path**: The path of the 'libtool' executable.

#### **MingwGccLinker**: A linker class for linking using gcc/g++ on Windows; inherits from GccLinker.

#### **MsvcLinker**: A linker class for linking using Microsoft's Visual C++ tools on Windows.

Properties (defaults set based on the path prefix passed to the constructor):
 * **dumpbin_exe**: The absolute path to the dumpbin executable.
 * **lib_exe**: The absolute path to the lib executable.
 * **link_exe**: The absolute path to the link executable.
 * **main_dumpbin_regex**: The compiled regex to use to search for a main() function in the dumpbin output.

Properties
----------
All properties are inherited from the parent scope if there is one.

 * **comments_regex**: The regex to use to match (and ignore) comments when using "simple" main() detection.
 * **main_function_regex**: The regex to use to detect a main() function when using "simple" main() detection.
  
 * **linker**: The linker instance used to link executables / shared libraries, and to create static libraries.
               This is set to link.GccLinker() by default on Linux, link.MingwGccLinker() by default on Windows, and link.OsxGccLinker() by default on OS X.
 * **shared_lib_ext**: The extension to use for shared libraries. The default is ".so" on Linux, ".dll" on Windows, and
                       ".dylib" on OS X.
 * **static_lib_ext**: The extension for static libraries. Set to ".a" by default.
 * **exe_ext**: The extension to use for exectuables. Set to "" (empty string) by default.
 * **lib_prefix**: The prefix to use for static/shared libraries. Set to "lib" by default.
 * **obj_ext**: The file extension for object files processed by the linker (eg ".o" for gcc or ".obj" for MSVC).  This property is
                read-only as its value is provided by the linker implementation.
  
 * **shared_libname**: The name to use for the generated shared library (if any). If set to None, the library name will
                       be &lt;lib_prefix>&lt;current directory name>&lt;shared_lib_ext>. The default value is None.
 * **static_libname**: The name to use for the generated lib_in_lib static library (if any). If set to None, the library name will
                       be &lt;lib_prefix>&lt;current directory name>_all&lt;static_lib_ext>. The default value is None.
                       Note that the regular static library (not lib_in_lib) is always named &lt;lib_prefix>&lt;current directory name>&lt;static_lib_ext>.
  
 * **detect_exe**: The method to use for executable detection (ie, if an object file exports a main() function).
                   If set to "exact", the link module uses the linker instance's 'contains_main_function' method
                   to determine if each object file contains a main() function. If set to "simple", the link module
                   will use the comments_regex and main_function_regex to determine if the source file that generated
                   each object file contains a main() function (note that this only applies to object files for which
                   the source is known, ie the contents of the 'objects' dict). If set to False/None, then no automatic
                   detection of executables will be performed. The default value is "exact".
 * **link_cxx**: If True, the link module will tell the linker instance to link C++ code. If False, the link will be done
                 for C code. The default value is False, but may be set to True by the c module if any C++ source files
                 are detected. Note that C++ mode will be used for linking if any of the library dependencies (from the
                 'depdirs' and 'projdirs' properties) contain C++ code.
 * **make_static_lib**: Whether or not to create a static library containing the non-executable object files.
                        The default value is True.
 * **make_shared_lib**: Whether or not to create a shared library containing the non-executable files (linked with all library dependencies).
                        The default value is False.
 * **strip**: Whether or not to strip the resulting shared library and/or executables. The default value is False.
 * **lib_in_lib**: If True (and a static library is being created), the link module will create an additional static library
                   named &lt;lib_prefix>&lt;current directory name>_all&lt;static_lib_ext> (or &lt;static_libname>, if set) which
                   contains the local library contents as well as the contents of all library dependencies from 'local_static_libs',
                   and transitively all 'static_libs', 'depdirs', and 'projdirs' libraries - ie the link module will recursively
                   gather all the static library dependencies from all the dependency directories. Useful for generating a
                   single static library for release that contains all of its dependencies.
 * **unique_names**: If True, the output libraries/executables will be named according to the path from the project directory, to avoid
                     naming conflicts when the build directory is not a relative path. The default value is False.
  
 * **exe_objs**: A list of object files to link into executables (without checking whether they contain a main() function).
 * **non_exe_objs**: A list of object files that should not be linked into an executable, even if they contain a main() function.
 * **objects**: A dict mapping &lt;object file>: &lt;source file>. This allows the link module to determine which source file
                was compiled to each object file when "simple" main detection is being used. Filled in by the c module.
 * **obj_nosrc**: A list of object files for which the source file is not known.
 * **non_lib_objs**: A list of object files which should not be linked into a library (static or shared).
  
 * **depdirs**: A list of directories that the object files in this directory depend on. The link module will instruct emk
                to recurse into these directories. When linking, the flags, static libs, and syslibs from these directory
                dependencies will be included in the link (including any from depdirs of the depdirs, and so on - the flags
                and libs are gathered transitively). It is acceptable to have circular dependencies in the depdirs.
 * **projdirs**: A list of dependency directories (like depdirs) that are resolved relative to the project directory.
  
 * **static_libs**: A list of paths to static libraries to link in (transitively included by links that depend on this directory).
                    Relative paths will be resolved relative to the current scope.
 * **local_static_libs**: A list of paths to static libraries to link in; not transitively included.
                          Relative paths will be resolved relative to the current scope.
 * **syslibs**: A list of library names to link in (like '-l&lt;name>.'). Transitively included by links that depend on this directory.
 * **local_syslibs**: A list of library names to link in; not transitively included.
 * **syslib_paths**: A list of directories to search for named libraries (ie syslibs). Transitively included by links that depend on this directory.
                     Relative paths will be resolved relative to the current scope.
 * **local_syslib_paths**: A list of directories to search for named libraries; not transitively included.
                           Relative paths will be resolved relative to the current scope.
  
 * **flags**: A list of additional flags to pass to the linker (transitively included by links that depend on this directory).
 * **local_flags**: A list of additional flags to pass to the linker; not transitively included.
 * **libflags**: A list of additional flags to pass to the linker when linking a shared library. Transitively included by links
                 that depend on this directory.
 * **local_libflags**: A list of additional flags to pass to the linker when linking a shared library; not transitively included.
 * **exeflags**: A list of additional flags to pass to the linker when linking an executable. Transitively included by links that depend on this directory.
 * **local_exeflags**: A list of additional flags to pass to the linker when linking an executable; not transitively included.

Linker Support
----------------

To add support for a new linker, you must implement a linker class for use by the link module (and then set the link module's `linker` property
to an instance of the linker class). A linker class must provide the following methods:

#### `contains_main_function(self, objfile)`
Determine if an object file contains a main() function. This is used by the link module to autodetect which object files
should be linked into executables. Returns True if the object file contains a main() function, False otherwise.

Arguments:
 * **objfile**: The path to the object file.

#### `create_static_lib(self, dest, source_objs, other_libs)`
Create a static library (archive) containing the given object files and all object files contained in
the given other libs (which will be static libraries as well).

Called by the link module to create a static library. If the link module's `lib_in_lib` property is True,
the link module will pass in the library dependencies of this library in the 'other_libs' argument. This
method must include the contents of all the other libraries in the generated static library.

Arguments:
 * **dest**: The path of the static library to generate.
 * **source_objs**: A list of paths to object files to include in the generated static library.
 * **other_libs**: A list of paths to other static libraries whose contents should be included in the generated static library.

#### `static_lib_cwd_safe(self)`
Returns True if creating a static library using the create_static_lib() method is cwd_safe (ie, does not
use anything that depends on the current working directory); returns False otherwise.

#### `shlib_opts(self)`
Returns a list of options that the link module should use when linking a shared library.

#### `exe_opts(self)`
Returns a list of options that the link module should use when linking an executable.

#### `do_link(self, dest, source_objs, abs_libs, lib_dirs, rel_libs, flags, cxx_mode)`
Link a shared library or executable. The link module does not order the libraries to be linked in, so this
method must ensure that any ordering dependencies are solved. The GCC linker uses the '--start-group' and
'--end-group' options to make sure that library ordering is not an issue; the documentation says that this
can be slow but in reality it makes very little difference.

Arguments:
 * **dest**: The path of the destination file to produce.
 * **source_objs**: A list of object files to link in.
 * **abs_libs**: A list of absolute paths to (static) libraries that should be linked in.
 * **lib_dirs**: Additional search paths for relative libraries.
 * **rel_libs**: Relative library names to link in.
 * **flags**: Additional flags to be passed to the linker.
 * **cxx_mode**: If True, then the object files or libraries contain C++ code (so use g++ to link, for example).

#### `link_cwd_safe(self)`
Returns True if linking a shared library or executable using the do_link() method is cwd_safe (ie, does not
use anything that depends on the current working directory); returns False otherwise.

#### `strip(self, path)`
Strip unnecessary symbols from the given shared library / executable. Called by the link module after linking
if its `strip` property is True.

Arguments:
 * **path**: The path of the file to strip.

#### `obj_ext(self)`
This function will be called to get the extension of object files consumed by this linker.
