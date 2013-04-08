---
title: "emk: C module"
layout: module
---

C Module
========

The c module is an emk module for automatically detecting and compiling C and C++ source code. This module adds the compiled object
files to the link module, which will link them into libraries/executables as desired. The object files are added to the link module's
`objects` property (each mapped to the source file that the object file was built from), so that the link module can autodetect main()
functions from the source (if `link.detect_exe` == "simple"). See the link module documentation for details of main() autodetection.

The c module also sets the link module's `link_cxx` flag if there are any C++ source files being compiled.

The C module can support various compilers by setting the `compiler` property to a different instance. The default compiler instance
uses GCC for compilation. You can create a new instance of `c.GccCompiler` to use a different version of gcc (eg for cross-compilation).
To support other compilers, you would need to implement a compiler class (see "Compiler Support" below).

This module defines emk rules during the prebuild stage, to allow autodiscovery of generated source files
from rules defined before the prebuild stage (ie, in the post_rules() method of other modules). See the
`autodetect` and `autodetect_from_targets` properties for more information about autodiscovery of source files.

Note that the compilation rules are not built automatically; the link module (or other modules/user code)
is responsible for marking the object files as autobuild if desired.

Classes
-------

#### **GccCompiler**: A compiler class that uses gcc/g++ to compile.

Properties (defaults set based on the path prefix passed to the constructor):

 * **c_path**: The path of the C compiler (eg "gcc").
 * **cxx_path**: The path of the C++ compiler (eg "g++").

#### **MsvcCompiler**: A compiler class that uses the Microsoft Visual Studio command line tools to compile.

Properties (defaults set based on the path prefix passed to the constructor):

 * **cl_exe**: The full path of the MSVC compiler (eg "C:\Program Files (x86)\Microsoft Visual Studio 10.0\VC\bin\cl.exe").

Properties
----------
All properties are inherited from the parent scope if there is one.

 * **compiler**: The compiler instance that is used to load dependencies and compile C/C++ code.
 * **include_dirs**: A list of additional include directories for both C and C++ code.
 * **defines**: A dict of &lt;name>: &lt;value> defines for both C and C++; each entry is equivalent to #define &lt;name> &lt;value>.
 * **flags**: A list of flags for both C and C++. If you have a 'flag' that is more than one argument,
   pass it as a tuple. Example: ("-isystem", "/path/to/extra/sys/includes"). Duplicate flags will be removed.
 * **source_files**: A list of files that should be included for compilation. Files will be built as C or C++ depending on the file extension.
  
 * **c.exts**: The list of file extensions (suffixes) that will be considered as C code. The default is [".c"].
 * **c.include_dirs**: A list of additional include directories for C code.
 * **c.defines**: A dict of &lt;name>: &lt;value> defines for C.
 * **c.flags**: A list of flags for C.
 * **c.source_files**: A list of C files that should be included for compilation (will be built as C code).
  
 * **cxx.exts**: The list of file extensions (suffixes) that will be considered as C++ code. The default is [".cpp", ".cxx", ".c++", ".cc"].
 * **cxx.include_dirs**: A list of additional include directories for C++ code.
 * **cxx.defines**: A dict of &lt;name>: &lt;value> defines for C++.
 * **cxx.flags**: A list of flags for C++.
 * **cxx.source_files**: A list of C++ files that should be included for compilation (will be built as C++ code).
  
 * **autodetect**: Whether or not to autodetect files to build from the scope directory. All files that match the
   c.exts suffixes will be compiled as C, and all files that match the cxx.exts suffixes will be
   compiled as C++. Autodetection does not take place until the prebuild stage, so that autodetection
   of generated code can gather as many targets as possible (see autodetect_from_targets).
   The default value is True.
 * **autodetect_from_targets**: Whether or not to autodetect generated code based on rules defined in the current scope. The default value is True.
 * **excludes**: A list of source files to exclude from compilation.
 * **non_lib_src**: A list of source files that will not be linked into a library for this directory (passed to the link module).
 * **non_exe_src**: A list of source files that will not be linked into an executable, even if they contain a main() function.
 * **unique_names**: If True, the output object files will be named according to the path from the project directory,
   to avoid naming conflicts when the build directory is not a relative path. The default value
   is False. If True, the link module's unique_names property will also be set to True.

 * **obj_ext**: The file extension for object files generated by the compiler (eg ".o" for gcc or ".obj" for MSVC).  This property is
   read-only as its value is provided by the compiler implementation.

Compiler Support
----------------

To add support for a new compiler, you must implement a compiler class for use by the c module (and then set the c module's `compiler` property
to an instance of the compiler class). A compiler class must provide the following methods:

#### `load_extra_dependencies(self, path)`
This function should load any extra dependencies for the given object file path (ie, header files). The extra dependencies could be loaded from a generated
dependency file for that path, or loaded from the emk.scope_cache(path) (or some other mechanism).

Arguments:

 * **path**: The path of the object file to get dependencies for.

#### `compile_c(self, source, dest, includes, defines, flags)`
This function will be called to compile a C file into an object file. This function is not required if no C files will be compiled.

Arguments:

 * **source**: The C source file path to compile.
 * **dest**: The output object file path.
 * **includes**: A list of extra include directories.
 * **defines**: A dict of &lt;name>: &lt;value> entries to be used as defines; each entry is equivalent to #define &lt;name> &lt;value>.
 * **flags**: A list of additional flags. This list may contain tuples; to flatten the list, you could use
   the emk utils module: `flattened = utils.flatten(flags)`.

#### `compile_cxx(self, source, dest, includes, defines, flags)`
This function will be called to compile a C++ file into an object file. This function is not required if no C++ files will be compiled.

Arguments:

 * **source**: The C++ source file path to compile.
 * **dest**: The output object file path.
 * **includes**: A list of extra include directories.
 * **defines**: A dict of &lt;name>: &lt;value> entries to be used as defines; each entry is equivalent to #define &lt;name> &lt;value>.
 * **flags**: A list of additional flags. This list may contain tuples; to flatten the list, you could use
   the emk utils module: `flattened = utils.flatten(flags)`.

#### `obj_ext(self)`
This function will be called to get the extension of object files built by this compiler.
