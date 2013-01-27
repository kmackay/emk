import os
import logging
import re
import shutil

log = logging.getLogger("emk.java")
utils = emk.module("utils")

need_depdirs = {}
dir_cache = {}

comments_regex = re.compile(r'(/\*.*?\*/)|(//.*?$)', re.MULTILINE | re.DOTALL)
package_regex = re.compile(r'package\s+(\S+)\s*;')
main_function_regex = re.compile(r'((public\s+static)|(static\s+public))\s+void\s+main\s*\(')

class Module(object):
    def __init__(self, scope, parent=None):
        self._abs_depdirs = set()
        self._classpaths = set()
        self._jar_contents = set()
        self._sysjars = set()
        
        self._class_dir = None
        self._resource_dir = None
        self._jar_dir = None
        
        self._depended_by = set()
        
        if parent:
            self.compile_flags = list(parent.compile_flags)
            self.exts = list(parent.exts)
            self.source_files = list(parent.source_files)
        
            self.autodetect = parent.autodetect
            self.autodetect_from_targets = parent.autodetect_from_targets
            self.excludes = list(parent.excludes)
            
            self.exe_classes = list(parent.exe_classes)
            self.exclude_exe_classes = list(parent.exclude_exe_classes)
            self.autodetect_exe = parent.autodetect_exe
            
            self.resources = list(parent.resources)
            
            self.make_jar = parent.make_jar
            self.jarname = parent.jarname
            self.jar_in_jar = parent.jar_in_jar
            self.exe_jar_in_jar = parent.exe_jar_in_jar
            
            self.depdirs = list(parent.depdirs)
            self.projdirs = list(parent.projdirs)
            self.sysjars = list(parent.sysjars)
            
            self.class_dir = parent.class_dir
            self.resource_dir = parent.resource_dir
            self.jar_dir = parent.jar_dir
        else:
            self.compile_flags = []
            self.exts = [".java"]
            self.source_files = []
        
            self.autodetect = True
            self.autodetect_from_targets = True
            self.excludes = []
            
            # exe_classes is a list of fully-qualified class names that contain valid main() methods.
            self.exe_classes = []
            self.exclude_exe_classes = []
            self.autodetect_exe = True
            
            # resources is a list of (source, jar-location) tuples/
            # source is the file that will be put into the jar
            # jar-location is the relative path that the file will be put into (relative to the jar root)
            self.resources = []
            
            self.make_jar = True
            self.jarname = None
            self.jar_in_jar = False
            self.exe_jar_in_jar = True
            
            self.depdirs = []
            self.projdirs = []
            self.sysjars = []
            
            self.class_dir = None
            self.resource_dir = None
            self.jar_dir = None
    
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
                package = pm.group(1).split('.')
        return (main, package)
    
    def _get_needed_by(self, d, result):
        global dir_cache
        result.add(d)
        for sub in dir_cache[d]._depended_by:
            if not sub in result:
                self._get_needed_by(sub, result)
    
    def _prebuild(self):
        global need_depdirs
        global dir_cache
        
        for d in self.projdirs:
            self.depdirs.append(os.path.join(emk.proj_dir, d))
        self.projdirs = []
        
        self._abs_depdirs = set([emk.abspath(d) for d in self.depdirs])
        
        for d in self._abs_depdirs:
            emk.recurse(d)
            
        sources = set()
        
        if self.autodetect:
            if self.autodetect_from_targets:
                target_files = [t for t in emk.local_targets.keys() if self._matches_exts(t, self.exts)]
                if target_files:
                    log.debug("Detected generated Java files: %s", target_files)
                    self.source_files.extend(target_files)
                    
            files = [f for f in os.listdir(emk.scope_dir) if os.path.isfile(f)]
            for file_path in files:
                if self._matches_exts(file_path, self.exts):
                    self.source_files.append(file_path)
        
        for f in self.source_files:
            if not f in self.excludes:
                sources.add(f)
                
        if self.autodetect_exe:
            for source in sources:
                m, p = self._examine_source(source)
                if m:
                    fname = os.path.basename(source)
                    name, ext = os.path.splitext(fname)
                    if p:
                        p.append(name)
                        fqn = '.'.join(p)
                    else:
                        fqn = name
                    self.exe_classes.append(fqn)
        exe_class_set = set(self.exe_classes)
        exe_class_set -= set(self.exclude_exe_classes)
        
        if self.resources:
            resource_set = set(self.resources)
            resource_sources, resource_dests = zip(*resource_set)
            emk.rule(["java.__jar_resources__"], resource_sources, self._copy_resources, threadsafe=True, ex_safe=True, args={"dests": resource_dests})
        else:
            utils.mark_virtual_rule(["java.__jar_resources__"], [])
        
        emk.rule(["java.__jar_contents__"], sources, self._build_classes, threadsafe=True)
        deps = [os.path.join(d, "java.__jar_contents__") for d in self._abs_depdirs]
        emk.depend("java.__jar_contents__", *deps)
        emk.depend("java.__jar_contents__", "java.__jar_resources__")
        
        self._class_dir = os.path.join(emk.scope_dir, emk.build_dir, "classes")
        if self.class_dir:
            self._class_dir = os.path.join(emk.scope_dir, self.class_dir)
        
        self._resource_dir = os.path.join(emk.scope_dir, emk.build_dir, "resources")
        if self.resource_dir:
            self._resource_dir = os.path.join(emk.scope_dir, self.resource_dir)
        
        self._jar_dir = os.path.join(emk.scope_dir, emk.build_dir)
        if self.jar_dir:
            self._jar_dir = os.path.join(emk.scope_dir, self.jar_dir)
        
        dirname = os.path.basename(emk.scope_dir)
        jarname = dirname + ".jar"
        if self.jarname:
            jarname = self.jarname
        jarpath = os.path.join(self._jar_dir, jarname)
        if self.make_jar:
            emk.rule([jarpath], ["java.__jar_contents__"], self._make_jar, threadsafe=True, ex_safe=True, args={"jar_in_jar": self.jar_in_jar})
            emk.alias(jarpath, jarname)
            emk.autobuild(jarpath)
        
        if exe_class_set:
            exe_jarpath = jarpath + "_exe"
            if self.make_jar and self.jar_in_jar == self.exe_jar_in_jar:
                exe_jarpath = jarpath
            else:
                emk.rule([exe_jarpath], ["java.__jar_contents__"], self._make_jar, threadsafe=True, ex_safe=True, args={"jar_in_jar": self.exe_jar_in_jar})
            for exe in exe_class_set:
                specific_jarname = exe + ".jar"
                specific_jarpath = os.path.join(self._jar_dir, specific_jarname)
                emk.rule([specific_jarpath], [exe_jarpath], self._make_exe_jar, threadsafe=True, ex_safe=True, args={"exe_class": exe})
                emk.alias(specific_jarpath, specific_jarname)
                emk.autobuild(specific_jarpath)
        
        self._classpaths = set([self._class_dir])
        self._jar_contents = set([self._class_dir, self._resource_dir])
        self._sysjars = set([emk.abspath(j) for j in self.sysjars])
        for d in self._abs_depdirs:
            if d in dir_cache:
                cache = dir_cache[d]
                self._classpaths |= cache._classpaths
                self._jar_contents |= cache._jar_contents
                self._sysjars |= cache._sysjars
                cache._depended_by.add(emk.scope_dir)
            elif d in need_depdirs:
                need_depdirs[d].add(emk.scope_dir)
            else:
                need_depdirs[d] = set([emk.scope_dir])
        
        needed_by = set()
        if emk.scope_dir in need_depdirs:
            for d in need_depdirs[emk.scope_dir]:
                self._depended_by.add(d)
                self._get_needed_by(d, needed_by)
        
        for d in needed_by:
            cache = dir_cache[d]
            cache._classpaths |= self._classpaths
            cache._jar_contents |= self._jar_contents
            cache._sysjars |= self._sysjars
        
        dir_cache[emk.scope_dir] = self
    
    def _copy_resources(self, produces, requires, args):
        for dest, src in zip(args["dests"], requires):
            d, n = os.path.split(dest)
            if not n:
                n = os.path.basename(src)
            dest_dir = os.path.join(self._resource_dir, d)
            utils.mkdirs(dest_dir)
            dest = os.path.join(dest_dir, n)
            utils.rm(dest)
            os.symlink(src, dest)
        
        emk.mark_virtual("java.__jar_resources__")
    
    def _build_classes(self, produces, requires, args):
        global dir_cache
        
        utils.mkdirs(self._class_dir)
    
        if requires:
            classpath = ':'.join(self._classpaths | self._sysjars)
    
            cmd = ["javac", "-d", self._class_dir, "-sourcepath", emk.scope_dir, "-classpath", classpath]
            cmd.extend(utils.flatten_flags(self.compile_flags))
            cmd.extend(requires)
            utils.call(*cmd)
        emk.mark_virtual("java.__jar_contents__")
        
    def _make_jar(self, produces, requires, args):
        jarfile = produces[0]
        
        dirset = set([self._class_dir, self._resource_dir])
        if args["jar_in_jar"]:
            dirset = self._jar_contents
        dirs = [(d, "") for d in dirset]
        
        entries = {}
        visited_dirs = set()
        while dirs:
            copy = dirs
            dirs = []
            for d, relpath in copy:
                if d in visited_dirs:
                    continue
                visited_dirs.add(d)
                
                if os.path.isdir(d):
                    subs = os.listdir(d)
                    for f in subs:
                        path = os.path.join(d, f)
                        if os.path.isfile(path):
                            entries[os.path.join(relpath, f)] = path
                        else:
                            dirs.append((path, os.path.join(relpath, f)))
        
        if entries:
            jarfile_contents = jarfile + ".contents"
            utils.rm(jarfile_contents)
            utils.mkdirs(jarfile_contents)
        
            for relpath, srcpath in entries.items():
                destpath = os.path.join(jarfile_contents, relpath)
                utils.mkdirs(os.path.dirname(destpath))
                os.symlink(srcpath, destpath)
        
            cmd = ["jar", "cf", jarfile, "-C", jarfile_contents, "."]
            try:
                utils.call(*cmd)
                utils.call("jar", "i", jarfile)
            except:
                utils.rm(jarfile)
                raise
        else:
            log.warning("Not making %s, since it has no contents", jarfile)
            emk.mark_virtual(jarfile)
    
    def _make_exe_jar(self, produces, requires, args):
        dest = produces[0]
        src = requires[0]
        try:
            shutil.copy2(src, dest)
        
            manifest = dest + ".manifest"
            with open(manifest, "w") as f:
                f.write("Main-Class: " + args["exe_class"] + '\n')
        
            utils.call("jar", "ufm", dest, manifest)
        except:
            utils.rm(dest)
            raise
        