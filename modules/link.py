import os
import sys
import logging
import re
import shutil

log = logging.getLogger("emk.link")

main_nm_regex = re.compile(r'\s+T\s+_main\s*$', re.MULTILINE)

utils = emk.module("utils")

class _GccLinker(object):
    def __init__(self, path_prefix=""):
        self.path_prefix = path_prefix
        self.c_path = path_prefix + "gcc"
        self.cxx_path = path_prefix + "g++"
        self.ar_path = path_prefix + "ar"
        self.strip_path = path_prefix + "strip"
        self.nm_path = path_prefix + "nm"
    
    def contains_main_function(self, objfile):
        out, err, code = utils.call(self.nm_path, "-g", objfile, print_call=False)
        if main_nm_regex.search(out):
            return True
        return False
        
    def extract_static_lib(self, lib):
        utils.call(self.ar_path, "x", lib, print_call=False)
    
    def add_to_static_lib(self, dest, objs):
        utils.call(self.ar_path, "r", dest, *objs)
    
    def create_static_lib(self, dest, source_objs, other_libs):
        objs = list(source_objs)
        orig_dir = os.getcwd()
        dump_dir = os.path.join(orig_dir, emk.build_dir, "__lib_temp__")
        utils.mkdirs(dump_dir)
        os.chdir(dump_dir)
        counter = 0
        for lib in other_libs:
            d = "%s" % (counter)
            os.mkdir(d)
            os.chdir(d)
            self.extract_static_lib(lib)
            files = [f for f in os.listdir(os.getcwd()) if os.path.isfile(f) and f.endswith(".o")]
            for file_path in files:
                name, ext = os.path.splitext(file_path)
                new_name = "%s_%s%s" % (name, counter, ext)
                os.rename(file_path, new_name)
                objs.append(os.path.realpath(os.path.abspath(new_name)))
            os.chdir(dump_dir)
            counter += 1
        os.chdir(orig_dir)
        
        utils.rm(dest)
        
        left = len(objs)
        start = 0
        while left > 32:
            cur_objs = objs[start:start+32]
            start += 32
            left -= 32
            self.add_to_static_lib(dest, cur_objs)
        cur_objs = objs[start:]
        self.add_to_static_lib(dest, cur_objs)
        
        shutil.rmtree(dump_dir, ignore_errors=True)
    
    def static_lib_threadsafe(self):
        return False
    
    def shlib_opts(self):
        return ["-shared"]
    
    def exe_opts(self):
        return []
    
    def link_cmd(self, cmd, flags, dest, objs, abs_libs, lib_dirs, libs):
        sg = "-Wl,--start-group"
        eg = "-Wl,--end-group"
        call = [cmd] + flags + ["-o", dest] + objs + [sg] + abs_libs + [eg] + lib_dirs + [sg] + libs + [eg]
        utils.call(*call)

    def do_link(self, dest, source_objs, abs_libs, lib_dirs, rel_libs, flags, cxx_mode=False):
        linker = self.c_path
        if cxx_mode:
            linker = self.cxx_path
        
        flat_flags = utils.flatten_flags(flags)
        
        lib_dir_flags = ["-L" + d for d in lib_dirs]
        rel_lib_flags = ["-l" + lib for lib in rel_libs]
        
        self.link_cmd(linker, flat_flags, dest, source_objs, abs_libs, lib_dir_flags, rel_lib_flags)
    
    def link_threadsafe(self):
        return True
        
    def strip(self, path):
        utils.call(self.strip_path, "-S", "-x", path)

class _OsxGccLinker(_GccLinker):
    def __init__(self, path_prefix=""):
        super(_OsxGccLinker, self).__init__(path_prefix)
        self.lipo_path = self.path_prefix + "lipo"
        self.libtool_path = self.path_prefix + "libtool"
    
    def extract_static_lib(self, lib):
        out, err, ret = utils.call(self.lipo_path, "-info", lib, print_call=False)
        if "is not a fat file" in out:
            utils.call(self.ar_path, "x", lib, print_call=False)
        else:
            start, mid, rest = out.partition(lib + " are:")
            archs = rest.strip().split(' ')
            current_dir = os.getcwd()
            objs = set()
            for arch in archs:
                os.mkdir(arch)
                utils.call(self.lipo_path, lib, "-thin", arch, "-output", os.path.join(arch, "thin.a"), print_call=False)
                os.chdir(arch)
                utils.call(self.ar_path, "x", "thin.a", print_call=False)
                objs.update([f for f in os.listdir(os.getcwd()) if os.path.isfile(f) and f.endswith(".o")])
                os.chdir(current_dir)
            for obj in objs:
                cmd = [self.lipo_path, "-create", "-output", obj]
                for arch in archs:
                    arch_obj = os.path.join(arch, obj)
                    if os.path.isfile(arch_obj):
                        cmd.append(arch_obj)
                utils.call(*cmd, print_call=False)

    def add_to_static_lib(self, dest, objs):
        cmd = [self.libtool_path, "-static", "-s", "-o", dest, "-"]
        if os.path.isfile(dest):
            cmd.append(dest)
        cmd.extend(objs)
        utils.call(*cmd)

    def shlib_opts(self):
        return ["-dynamiclib"]
    
    def link_cmd(self, cmd, flags, dest, objs, abs_libs, lib_dirs, libs):
        call = [cmd] + flags + ["-o", dest] + objs + abs_libs + lib_dirs + libs
        utils.call(*call)
        
link_cache = {}
need_depdirs = {}
comments_regex = re.compile(r'(/\*.*?\*/)|(//.*?$)', re.MULTILINE | re.DOTALL)
main_function_regex = re.compile(r'int\s+main\s*\(')

class Module(object):
    def __init__(self, scope, parent=None):
        self.GccLinker = _GccLinker
        self.OsxGccLinker = _OsxGccLinker
        
        self._all_depdirs = set()
        self._depended_by = set()
        self._all_static_libs = set()
        self._static_libpath = None
        
        if parent:
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
            
            self.exe_objs = parent.exe_objs.copy()
            self.non_exe_objs = parent.non_exe_objs
            self.objects = parent.objects.copy()
            self.obj_nosrc = parent.obj_nosrc.copy()
            
            self.flags = parent.flags.copy()
            self.local_flags = list(parent.local_flags)
            self.libflags = parent.libflags.copy()
            self.local_libflags = list(parent.local_libflags)
            self.exeflags = parent.exeflags.copy()
            self.local_exeflags = list(parent.local_exeflags)
            
            self.static_libs = parent.static_libs.copy()
            self.local_static_libs = parent.local_static_libs.copy()
            
            self.depdirs = parent.depdirs.copy()
            self.projdirs = parent.projdirs.copy()
            self.syslibs = parent.syslibs.copy()
            self.syslib_paths = parent.syslib_paths.copy()
        else:
            if sys.platform == "darwin":
                self.linker = _OsxGccLinker()
                self.shared_lib_ext = ".dylib"
            else:
                self.linker = _GccLinker()
                self.shared_lib_ext = ".so"
            self.static_lib_ext = ".a"
            self.exe_ext = ""
            self.lib_prefix = "lib"
            
            self.shared_libname = None
            self.static_libname = None
            
            self.detect_exe = "exact" # could also be "simple" or False
            self.link_cxx = False
            self.make_static_lib = True
            self.make_shared_lib = False
            self.strip = False
            self.lib_in_lib = False
            
            self.exe_objs = set()
            self.non_exe_objs = set()
            self.objects = {}
            self.obj_nosrc = set()
            
            self.flags = set()
            self.local_flags = []
            self.libflags = set()
            self.local_libflags = []
            self.exeflags = set()
            self.local_exeflags = []
            
            self.static_libs = set()
            self.local_static_libs = set()
            
            self.depdirs = set()
            self.projdirs = set()
            self.syslibs = set()
            self.syslib_paths = set()
    
    def new_scope(self, scope):
        return Module(scope, parent=self)
    
    def post_rules(self):
        if not emk.cleaning:
            emk.do_prebuild(self._prebuild)
    
    def _get_needed_by(self, d, result):
        global link_cache
        result.add(d)
        for sub in link_cache[d]._depended_by:
            if not sub in result:
                self._get_needed_by(sub, result)
    
    def _prebuild(self):
        global link_cache
        global need_depdirs
        
        self.syslib_paths = set([emk.abspath(d) for d in self.syslib_paths])
        
        for d in self.projdirs:
            self.depdirs.add(os.path.join(emk.proj_dir, d))
        self.projdirs.clear()

        self._all_static_libs.update(self.static_libs)
        
        for d in self.depdirs:
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
            emk.depend(os.path.join(d, "link.__exe_deps__"), *lib_deps)
            emk.depend(os.path.join(d, "link.__exe_deps__"), *self._all_static_libs)
        
        if self.detect_exe == "exact":
            emk.require_rule("link.__static_lib__", "link.__lib_in_lib__", "link.__shared_lib__", "link.__exe_deps__", "link.__exes__")
            emk.do_prebuild(self._create_interim_rule)
            emk.do_postbuild(self._create_rules)
        else:
            emk.do_prebuild(self._create_rules)
        
        link_cache[emk.scope_dir] = self
    
    def _create_interim_rule(self):
        all_objs = self.obj_nosrc | set([obj for obj, src in self.objects.items()]) | self.exe_objs
        emk.rule(["link.__interim__"], all_objs, utils.mark_exists, threadsafe=True)
        emk.build("link.__interim__")
        
    def _simple_detect_exe(self, sourcefile):
        with open(sourcefile) as f:
            data = f.read()
            text = comments_regex.sub('', data)
            if main_function_regex.search(text):
                return True
            return False

    def _create_rules(self):
        global link_cache
        
        exe_objs = self.exe_objs
        all_objs = self.obj_nosrc | set([obj for obj, src in self.objects.items()])
        
        if not self.detect_exe:
            pass
        elif self.detect_exe.lower() == "simple":
            for obj, src in self.objects.items():
                if (not obj in exe_objs) and (not obj in self.non_exe_objs) and self._simple_detect_exe(src):
                    exe_objs.add(obj)
        elif self.detect_exe.lower() == "exact":
            for obj, src in self.objects.items():
                if (not obj in exe_objs) and (not obj in self.non_exe_objs) and self.linker.contains_main_function(obj):
                    exe_objs.add(obj)
            for obj in self.obj_nosrc:
                if (not obj in exe_objs) and (not obj in self.non_exe_objs) and self.linker.contains_main_function(obj):
                    exe_objs.add(obj)
        
        lib_objs = all_objs - exe_objs
        
        emk.rule(["link.__exe_deps__"], ["link.__static_lib__"], utils.mark_exists, threadsafe=True)
        
        lib_deps = [os.path.join(d, "link.__static_lib__") for d in self._all_depdirs]
        emk.depend("link.__exe_deps__", *lib_deps)
        emk.depend("link.__exe_deps__", *self._all_static_libs)
        
        dirname = os.path.basename(emk.scope_dir)
        making_static_lib = False
        if lib_objs:
            if self.make_static_lib:
                making_static_lib = True
                libname = "lib" + dirname + self.static_lib_ext
                libpath = os.path.join(emk.build_dir, libname)
                self._static_libpath = libpath
                emk.rule([libpath], lib_objs, self._create_static_lib, threadsafe=self.linker.static_lib_threadsafe(), args={"all_libs": False})
                emk.alias(libpath, "link.__static_lib__")
                emk.build(libpath)
                
                if self.lib_in_lib:
                    libname = "lib" + dirname + "_all" + self.static_lib_ext
                    if self.static_libname:
                        libname = self.static_libname
                    libpath = os.path.join(emk.build_dir, libname)
                    emk.rule([libpath], ["link.__static_lib__", "link.__exe_deps__"], self._create_static_lib, threadsafe=self.linker.static_lib_threadsafe(), args={"all_libs": True})
                    emk.alias(libpath, "link.__lib_in_lib__")
                    emk.build(libpath)
            if self.make_shared_lib:
                libname = "lib" + dirname + self.shared_lib_ext
                if self.shared_libname:
                    libname = self.shared_libname
                libpath = os.path.join(emk.build_dir, libname)
                emk.rule([libpath], ["link.__exe_deps__"] + list(lib_objs), self._create_shared_lib, threadsafe=self.linker.link_threadsafe())
                emk.build(libpath)
                emk.alias(libpath, "link.__shared_lib__")
        if not making_static_lib:
            emk.rule(["link.__static_lib__"], [], utils.mark_exists, threadsafe=True)
        
        exe_targets = []
        exe_names = set()
        for obj in exe_objs:
            basename = os.path.basename(obj)
            n, ext = os.path.splitext(basename)
            name = n
            c = 1
            while name in exe_names:
                name = "%s_%s" % (n, c)
                c += 1
            exe_names.add(name)
            name = name + self.exe_ext
            
            path = os.path.join(emk.build_dir, name)
            emk.rule([path], [obj, "link.__exe_deps__"], self._create_exe, threadsafe=self.linker.link_threadsafe())
            emk.alias(path, name)
            exe_targets.append(path)
            
        emk.rule(["link.__exes__"], exe_targets, utils.mark_exists, threadsafe=True)
        emk.build("link.__exes__")
    
    def _create_static_lib(self, produces, requires, args):
        global link_cache
        
        objs = []
        other_libs = set()
        if args["all_libs"]:
            other_libs.add(emk.abspath(self._static_libpath))
            other_libs |= self.local_static_libs
            other_libs |= self.static_libs
            for d in self._all_depdirs:
                cache = link_cache[d]
                if cache._static_libpath:
                    other_libs.add(os.path.join(d, cache._static_libpath))
                other_libs |= cache.static_libs
        else:
            objs = requires
        
        self.linker.create_static_lib(produces[0], objs, other_libs)
    
    def _create_shared_lib(self, produces, requires, args):
        global link_cache
        
        flags = self.linker.shlib_opts() + self.local_flags + self.local_libflags
        flagset = self.flags | self.libflags

        abs_libs = self.local_static_libs | self.static_libs
        syslibs = self.syslibs.copy()
        lib_paths = self.syslib_paths.copy()
        link_cxx = self.link_cxx
        
        for d in self._all_depdirs:
            cache = link_cache[d]
            flagset |= cache.flags
            flagset |= cache.libflags
            if cache._static_libpath:
                abs_libs.add(os.path.join(d, cache._static_libpath))
            abs_libs |= cache.static_libs
            syslibs |= cache.syslibs
            lib_paths |= cache.syslib_paths
            link_cxx = link_cxx or cache.link_cxx

        flags.extend(flagset)
        self.linker.do_link(produces[0], [o for o in requires if o.endswith('.o')], list(abs_libs), \
            lib_paths, syslibs, utils.unique_list(flags), cxx_mode=link_cxx)
    
    def _create_exe(self, produces, requires, args):
        global link_cache
        
        flags = self.linker.exe_opts() + self.local_flags + self.local_exeflags
        flagset = self.flags | self.exeflags

        abs_libs = self.local_static_libs | self.static_libs
        if self._static_libpath:
            abs_libs.add(emk.abspath(self._static_libpath))
        syslibs = self.syslibs.copy()
        lib_paths = self.syslib_paths.copy()
        link_cxx = self.link_cxx
        
        for d in self._all_depdirs:
            cache = link_cache[d]
            flagset |= cache.flags
            flagset |= cache.exeflags
            if cache._static_libpath:
                abs_libs.add(os.path.join(d, cache._static_libpath))
            abs_libs |= cache.static_libs
            syslibs |= cache.syslibs
            lib_paths |= cache.syslib_paths
            link_cxx = link_cxx or cache.link_cxx

        flags.extend(flagset)
        self.linker.do_link(produces[0], [o for o in requires if o.endswith('.o')], list(abs_libs), \
            lib_paths, syslibs, utils.unique_list(flags), cxx_mode=link_cxx)
