import os
import logging
import shlex

log = logging.getLogger("emk.c")

utils = emk.module("utils")

class _GccCompiler(object):
    def __init__(self, path_prefix=""):
        self.c_path = path_prefix + "gcc"
        self.cxx_path = path_prefix + "g++"
    
    def load_extra_dependencies(self, path):
        try:
            with open(path) as f:
                items = [s for s in f.read().split('\n') if s]
                return items
        except IOError:
            pass
    
    def depfile_args(self, dep_file):
        return ["-Wp,-MMD,%s" % (dep_file)]
    
    def compile(self, exe, source, dest, dep_file, includes, defines, flags):
        args = [exe]
        args.extend(self.depfile_args(dep_file))
        args.extend(["-I%s" % (emk.abspath(d)) for d in includes])
        args.extend(["-D%s=%s" % (key, value) for key, value in defines.items()])
        args.extend(utils.flatten_flags(flags))
        args.extend(["-o", dest, "-c", source])
        utils.call(args)
        
        try:
            with open(dep_file, "r+") as f:
                data = f.read()
                data = data.replace("\\\n", "")
                items = shlex.split(data)
                unique_items = set(items[2:])
                f.seek(0)
                f.truncate(0)
                f.write('\n'.join(unique_items))
        except IOError:
            log.error("Failed to fix up depfile %s", dep_file)
            utils.rm(dep_file)
        
    def compile_c(self, source, dest, dep_file, includes, defines, flags):
        self.compile(self.c_path, source, dest, dep_file, includes, defines, flags)
    
    def compile_cxx(self, source, dest, dep_file, includes, defines, flags):
        self.compile(self.cxx_path, source, dest, dep_file, includes, defines, flags)

class Module(object):
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

            self.c.include_dirs = list(parent.c.include_dirs)
            self.c.defines = parent.c.defines.copy()
            self.c.flags = list(parent.c.flags)
            self.c.exts = list(parent.c.exts)
            self.c.source_files = list(parent.c.source_files)
            
            self.cxx.include_dirs = list(parent.cxx.include_dirs)
            self.cxx.defines = parent.cxx.defines.copy()
            self.cxx.flags = list(parent.cxx.flags)
            self.cxx.exts = list(parent.cxx.exts)
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
        c_args = {"c++":False, "includes":c_includes, "defines":c_defines, "flags":c_flags}
        
        cxx_includes = utils.unique_list(self.include_dirs + self.cxx.include_dirs)
        cxx_flags = utils.unique_list(self.flags + self.cxx.flags)
        cxx_defines = dict(self.defines)
        cxx_defines.update(self.cxx.defines)
        cxx_args = {"c++":True, "includes":cxx_includes, "defines":cxx_defines, "flags":cxx_flags}
        
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
        
        emk.rule([dest], requires, self.do_compile, args=args, threadsafe=True, ex_safe=True)
        if extra_deps:
            emk.weak_depend(dest, extra_deps)
    
    def do_compile(self, produces, requires, args):
        if not self.compiler:
            raise emk.BuildError("No compiler defined!")
        try:
            if args["c++"]:
                self.compiler.compile_cxx(requires[0], produces[0], produces[0] + ".dep", args["includes"], args["defines"], args["flags"])
            else:
                self.compiler.compile_c(requires[0], produces[0], produces[0] + ".dep", args["includes"], args["defines"], args["flags"])
        except:
            utils.rm(produces[0])
            utils.rm(produces[0] + ".dep")
            raise
        