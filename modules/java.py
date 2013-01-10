import os
import logging
import shlex

log = logging.getLogger("emk.java")

utils = emk.module("utils")

class Module(object):
    def __init__(self, scope, parent=None):
        
        if parent:
            self.compile_flags = list(parent.compile_flags)
            self.exts = parent.exts.copy()
            self.source_files = parent.source_files.copy()
        
            self.autodetect = parent.autodetect
            self.autodetect_from_targets = parent.autodetect_from_targets
            self.excludes = parent.excludes.copy()
            
            self.depdirs = parent.depdirs.copy()
            self.projdirs = parent.projdirs.copy()
            self.syslibs = parent.syslibs.copy()
            self.syslib_paths = parent.syslib_paths.copy()
        else:
            self.compile_flags = []
            self.exts = set([".java"])
            self.source_files = set()
        
            self.autodetect = True
            self.autodetect_from_targets = True
            self.excludes = set()
            
            self.depdirs = set()
            self.projdirs = set()
            self.syslibs = set()
            self.syslib_paths = set()
    
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
        sources = set()
        
        if self.autodetect:
            files = [f for f in os.listdir(emk.current_dir) if os.path.isfile(f)]
            if self.autodetect_from_targets:
                target_files = [t for t in emk.local_targets.keys()]
                files.extend(target_files)
            for file_path in files:
                if self._matches_exts(file_path, self.exts):
                    self.source_files.add(file_path)
        
        for f in self.source_files:
            if f in self.excludes:
                continue
            sources.add(f)

        # TODO
