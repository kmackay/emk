import os
import logging
import re

log = logging.getLogger("emk.java")
utils = emk.module("utils")

dir_cache = {}

comments_regex = re.compile(r'(/\*.*?\*/)|(//.*?$)', re.MULTILINE | re.DOTALL)
package_regex = re.compile(r'package\s+(\S+)\s*;')
main_function_regex = re.compile(r'((public\s+static)|(static\s+public))\s+void\s+main\s*\(')

class Module(object):
    def __init__(self, scope, parent=None):
        self._abs_depdirs = set()
        self._classpaths = set()
        self._sysjars = set()
        
        if parent:
            self.compile_flags = list(parent.compile_flags)
            self.exts = parent.exts.copy()
            self.source_files = parent.source_files.copy()
        
            self.autodetect = parent.autodetect
            self.autodetect_from_targets = parent.autodetect_from_targets
            self.excludes = parent.excludes.copy()
            
            self.exe_classes = parent.exe_classes.copy()
            self.autodetect_exe = parent.autodetect_exe
            
            self.make_jar = parent.make_jar
            self.jarname = parent.jarname
            self.jar_in_jar = parent.jar_in_jar
            self.exe_jar_in_jar = parent.exe_jar_in_jar
            
            self.depdirs = parent.depdirs.copy()
            self.projdirs = parent.projdirs.copy()
            self.sysjars = parent.sysjars.copy()
            
            self.class_dir = parent.class_dir
        else:
            self.compile_flags = []
            self.exts = set([".java"])
            self.source_files = set()
        
            self.autodetect = True
            self.autodetect_from_targets = True
            self.excludes = set()
            
            # exe_classes is a set of fully-qualified class names that contain valid main() methods.
            self.exe_classes = set()
            self.autodetect_exe = True
            
            self.make_jar = True
            self.jarname = None
            self.jar_in_jar = False
            self.exe_jar_in_jar = True
            
            self.depdirs = set()
            self.projdirs = set()
            self.sysjars = set()
            
            self.class_dir = None
    
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
    
    def _examine_source(self, sourcefile):
        global comments_regex
        package = None
        main = False
        with open(sourcefile) as f:
            data = f.read()
            text = comments_regex.sub('', data)
            if main_function_regex.search(text):
                main = True
            pm = package_regex.search(text)
            if pm:
                package = pm.group(1)
        return (main, package)
    
    def _prebuild(self):
        global dir_cache
        
        for d in self.projdirs:
            self.depdirs.add(os.path.join(emk.proj_dir, d))
        self.projdirs.clear()
        
        for d in self.depdirs:
            emk.recurse(d)
        
        self._abs_depdirs = set([emk.abspath(d) for d in self.depdirs])
            
        sources = set()
        
        if self.autodetect:
            if self.autodetect_from_targets:
                target_files = [t for t in emk.local_targets.keys() if self._matches_exts(t, self.exts)]
                if target_files:
                    log.debug("Detected generated Java files: %s", target_files)
                    self.source_files.update(target_files)
                    
            files = [f for f in os.listdir(emk.current_dir) if os.path.isfile(f)]
            for file_path in files:
                if self._matches_exts(file_path, self.exts):
                    self.source_files.add(file_path)
        
        for f in self.source_files:
            if not f in self.excludes:
                sources.add(f)
        
        for source in sources:
            m, p = self._examine_source(source)
            print "%s: main = %s, package = %s" % (source, m, p)
        
        emk.rule(["java.__classes__"], sources, self.build_classes, threadsafe=True)
        deps = [os.path.join(d, "java.__classes__") for d in self._abs_depdirs]
        emk.depend("java.__classes__", *deps)
        
        # if self.make_jar or there are any exes, then make a jar
        # if jar_in_jar is True, then add all depdirs and sysjars to the jar as well (recommended for exes)
        # make as few jars as possible
        dir_cache[emk.current_dir] = self
    
    def build_classes(self, produces, requires, args):
        global dir_cache
        
        class_dir = os.path.join(emk.current_dir, emk.build_dir)
        if self.class_dir:
            class_dir = os.path.join(emk.current_dir, self.class_dir)
        utils.mkdirs(class_dir)
        
        self._classpaths = set([class_dir])
        self._sysjars = set([emk.abspath(j) for j in self.sysjars])
        for d in self._abs_depdirs:
            cache = dir_cache[d]
            self._classpaths |= cache._classpaths
            self._sysjars |= cache._sysjars
        
        classpath = ':'.join(self._classpaths | self._sysjars)
        
        cmd = ["javac", "-d", class_dir, "-sourcepath", emk.current_dir, "-classpath", classpath]
        cmd.extend(utils.flatten_flags(self.compile_flags))
        cmd.extend(requires)
        utils.call(*cmd)
        
        emk.mark_exists("java.__classes__")
