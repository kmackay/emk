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
                data = f.read()
                data = data.replace("\\\n", "")
                items = shlex.split(data)
                return items[2:] # we just want the included files
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
        utils.call(*args)
        
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
            
            self.include_dirs = parent.include_dirs.copy()
            self.defines = parent.defines.copy()
            self.flags = list(parent.flags)

            self.c.include_dirs = parent.c.include_dirs.copy()
            self.c.defines = parent.c.defines.copy()
            self.c.flags = list(parent.c.flags)
            self.c.exts = parent.c.exts.copy()
            self.c.source_files = parent.c.source_files.copy()
            
            self.cxx.include_dirs = parent.cxx.include_dirs.copy()
            self.cxx.defines = parent.cxx.defines.copy()
            self.cxx.flags = list(parent.cxx.flags)
            self.cxx.exts = parent.cxx.exts.copy()
            self.cxx.source_files = parent.cxx.source_files.copy()
            
            self.autodetect = parent.autodetect
            self.excludes = parent.excludes.copy()
        else:
            self.compiler = _GccCompiler()
            
            self.include_dirs = set()
            self.defines = {}
            self.flags = []
            
            self.c.include_dirs = set()
            self.c.defines = {}
            self.c.flags = []
            self.c.exts = set([".c"])
            self.c.source_files = set()
            
            self.cxx.include_dirs = set()
            self.cxx.defines = {}
            self.cxx.flags = []
            self.cxx.exts = set([".cpp", ".cxx", ".c++", ".cc"])
            self.cxx.source_files = set()
        
            self.autodetect = True
            self.excludes = set()
    
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
        
        if self.autodetect:
            files = [f for f in os.listdir(emk.current_dir) if os.path.isfile(f)]
            for file_path in files:
                if self._matches_exts(file_path, self.c.exts):
                    self.c.source_files.add(file_path)
                if self._matches_exts(file_path, self.cxx.exts):
                    self.cxx.source_files.add(file_path)
                    
        emk.do_prebuild(self._prebuild)
    
    def _prebuild(self):
        c_sources = set()
        cxx_sources = set()
        for f in self.c.source_files:
            if f in self.excludes:
                continue
            c_sources.add(f)
        for f in self.cxx.source_files:
            if f in self.excludes:
                continue
            cxx_sources.add(f)
        
        c_includes = self.include_dirs | self.c.include_dirs
        c_flags = utils.unique_list(self.flags + self.c.flags)
        c_defines = dict(self.defines)
        c_defines.update(self.c.defines)
        c_args = {"c++":False, "includes":c_includes, "defines":c_defines, "flags":c_flags}
        
        cxx_includes = self.include_dirs | self.cxx.include_dirs
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
        
        dest = os.path.join(emk.build_dir, name + ".o")
        requires = [src]
        extra_deps = None
        if self.compiler:
            extra_deps = self.compiler.load_extra_dependencies(emk.abspath(dest + ".dep"))
        if not extra_deps is None:
            requires.extend(extra_deps)
            emk.allow_nonexistent(*extra_deps)
        else:
            requires.append(emk.ALWAYS_BUILD)
        
        emk.rule([dest], requires, self.do_compile, args=args, threadsafe=True)
    
    def do_compile(self, produces, requires, args):
        if not self.compiler:
            raise emk.BuildError("No compiler defined!")
        if args["c++"]:
            self.compiler.compile_cxx(requires[0], produces[0], produces[0] + ".dep", args["includes"], args["defines"], args["flags"])
        else:
            self.compiler.compile_c(requires[0], produces[0], produces[0] + ".dep", args["includes"], args["defines"], args["flags"])
        