import os
import logging
import re
import shutil

log = logging.getLogger("emk.java")
utils = emk.module("utils")

need_depdirs = {}
dir_cache = {}
sysjar_cache = {}

comments_regex = re.compile(r'(/\*.*?\*/)|(//.*?$)', re.MULTILINE | re.DOTALL)
package_regex = re.compile(r'package\s+(\S+)\s*;')
main_function_regex = re.compile(r'((public\s+static)|(static\s+public))\s+void\s+main\s*\(')

class Module(object):
    def __init__(self, scope, parent=None):
        self._abs_depdirs = set()
        self._local_classpath = None
        self._classpaths = set()
        self._sysjars = set()
        self._output_dir = None
        self._depended_by = set()
        
        if parent:
            self.compile_flags = list(parent.compile_flags)
            self.exts = parent.exts.copy()
            self.source_files = parent.source_files.copy()
        
            self.autodetect = parent.autodetect
            self.autodetect_from_targets = parent.autodetect_from_targets
            self.excludes = parent.excludes.copy()
            
            self.exe_classes = parent.exe_classes.copy()
            self.exclude_exe_classes = parent.exclude_exe_classes.copy()
            self.autodetect_exe = parent.autodetect_exe
            
            self.resources = parent.resources.copy()
            
            self.make_jar = parent.make_jar
            self.jarname = parent.jarname
            self.jar_in_jar = parent.jar_in_jar
            self.exe_jar_in_jar = parent.exe_jar_in_jar
            
            self.depdirs = parent.depdirs.copy()
            self.projdirs = parent.projdirs.copy()
            self.sysjars = parent.sysjars.copy()
            
            self.output_dir = parent.output_dir
        else:
            self.compile_flags = []
            self.exts = set([".java"])
            self.source_files = set()
        
            self.autodetect = True
            self.autodetect_from_targets = True
            self.excludes = set()
            
            # exe_classes is a set of fully-qualified class names that contain valid main() methods.
            self.exe_classes = set()
            self.exclude_exe_classes = set()
            self.autodetect_exe = True
            
            # resources is a set of (source, jar-location) tuples/
            # source is the file that will be put into the jar
            # jar-location is the relative path that the file will be put into (relative to the jar root)
            self.resources = set()
            
            self.make_jar = True
            self.jarname = None
            self.jar_in_jar = False
            self.exe_jar_in_jar = True
            
            self.depdirs = set()
            self.projdirs = set()
            self.sysjars = set()
            
            self.output_dir = None
    
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
        global sysjar_cache
        
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
                    self.exe_classes.add(fqn)
        self.exe_classes -= self.exclude_exe_classes
        
        if self.resources:
            resource_sources, resource_dests = zip(*self.resources)
            emk.rule(["java.__jar_resources__"], resource_sources, self._copy_resources, threadsafe=True, args={"dests": resource_dests})
        else:
            emk.rule(["java.__jar_resources__"], [], utils.mark_exists, threadsafe=True)
        
        emk.rule(["java.__jar_contents__"], sources, self._build_classes, threadsafe=True)
        deps = [os.path.join(d, "java.__jar_contents__") for d in self._abs_depdirs]
        emk.depend("java.__jar_contents__", *deps)
        emk.depend("java.__jar_contents__", "java.__jar_resources__")
        
        self._output_dir = os.path.join(emk.current_dir, emk.build_dir)
        if self.output_dir:
            self._output_dir = os.path.join(emk.current_dir, self.output_dir)
        
        expand_targets = []
        c = 0
        for jar in self.sysjars:
            jarpath = emk.abspath(jar)
            if not jarpath in sysjar_cache:
                sysjar_cache[jarpath] = os.path.join(self._output_dir, "expanded_jars", c)
                target = jarpath + ".__expanded__"
                emk.rule([target], jarpath, self._expand_jar, threadsafe=False)
                expand_targets.append(target)
                c += 1
        
        emk.rule(["java.__expanded_deps__"], expand_targets, utils.mark_exists, threadsafe=True)
        deps = [os.path.join(d, "java.__expanded_deps__") for d in self._abs_depdirs]
        emk.depend("java.__expanded_deps__", *deps)
        
        dirname = os.path.basename(emk.current_dir)
        jarname = dirname + ".jar"
        if self.jarname:
            jarname = self.jarname
        jarpath = os.path.join(self._output_dir, "jars", jarname)
        if self.make_jar:
            emk.rule([jarpath], ["java.__jar_contents__", "java.__expanded_deps__"], self._make_jar, threadsafe=True, args={"jar_in_jar": self.jar_in_jar})
            emk.alias(jarpath, jarname)
            emk.build(jarpath)
        
        if self.exe_classes:
            exe_jarpath = jarpath + "_exe"
            if self.make_jar and self.jar_in_jar == self.exe_jar_in_jar:
                exe_jarpath = jarpath
            else:
                emk.rule([exe_jarpath], ["java.__jar_contents__", "java.__expanded_deps__"], self._make_jar, threadsafe=True, args={"jar_in_jar": self.exe_jar_in_jar})
            for exe in self.exe_classes:
                specific_jarname = exe + ".jar"
                specific_jarpath = os.path.join(self._output_dir, "jars", specific_jarname)
                emk.rule([specific_jarpath], [exe_jarpath], self._make_exe_jar, threadsafe=True, args={"exe_class": exe})
                emk.alias(specific_jarpath, specific_jarname)
                emk.build(specific_jarpath)
        
        class_dir = os.path.join(self._output_dir, "jar_contents")
        self._local_classpath = class_dir
        self._classpaths = set([class_dir])
        self._sysjars = set([emk.abspath(j) for j in self.sysjars])
        for d in self._abs_depdirs:
            if d in dir_cache:
                cache = dir_cache[d]
                self._classpaths |= cache._classpaths
                self._sysjars |= cache._sysjars
                cache._depended_by.add(emk.current_dir)
            elif d in need_depdirs:
                need_depdirs[d].add(emk.current_dir)
            else:
                need_depdirs[d] = set([emk.current_dir])
        
        needed_by = set()
        if emk.current_dir in need_depdirs:
            for d in need_depdirs[emk.current_dir]:
                self._depended_by.add(d)
                self._get_needed_by(d, needed_by)
        
        for d in needed_by:
            cache = dir_cache[d]
            cache._classpaths |= self._classpaths
            cache._sysjars |= self._sysjars
        
        dir_cache[emk.current_dir] = self
    
    def _copy_resources(self, produces, requires, args):
        for dest, src in zip(args["dests"], requires):
            d, n = os.path.split(dest)
            if not n:
                n = os.path.basename(src)
            dest_dir = os.path.join(self._local_classpath, d)
            utils.mkdirs(dest_dir)
            os.symlink(src, os.path.join(dest_dir, n))
        
        emk.mark_exists("java.__jar_resources__")
    
    def _build_classes(self, produces, requires, args):
        global dir_cache
        
        utils.mkdirs(self._local_classpath)
        
        if requires:
            classpath = ':'.join(self._classpaths | self._sysjars)
        
            cmd = ["javac", "-d", self._local_classpath, "-sourcepath", emk.current_dir, "-classpath", classpath]
            cmd.extend(utils.flatten_flags(self.compile_flags))
            cmd.extend(requires)
            utils.call(*cmd)
        
        emk.mark_exists("java.__jar_contents__")

    def _expand_jar(self, produces, requires, args):
        jarfile = requires[0]
        expand_dir = sysjar_cache[jarfile]
        utils.mkdirs(expand_dir)
        os.chdir(expand_dir)
        utils.call("jar", "xf", jarfile)
        shutil.rmtree("META-INF", ignore_errors=True)
        emk.mark_exists(*produces)
        
    def _make_jar(self, produces, requires, args):
        global sysjar_cache
        
        jar_dir = os.path.join(self._output_dir, "jars")
        utils.mkdirs(jar_dir)
        
        jarfile = produces[0]
        
        dirs = set([self._local_classpath])
        if args["jar_in_jar"]:
            dirs |= self._classpaths
            for jar in self._sysjars:
                dirs.add(sysjar_cache[jarpath])
        
        cmd = ["jar", "cvf", jarfile]
        have_contents = False
        for d in dirs:
            entries = os.listdir(d)
            if entries:
                for entry in entries:
                    cmd.extend(["-C", d, entry])
                have_contents = True
        
        if have_contents:
            utils.call(*cmd, print_stdout=True)
            utils.call("jar", "i", jarfile)
        else:
            log.warning("Not making %s, since it has no contents", jarfile)
            emk.mark_exists(jarfile)
    
    def _make_exe_jar(self, produces, requires, args):
        dest = produces[0]
        src = requires[0]
        shutil.copy2(src, dest)
        
        manifest = dest + ".manifest"
        with open(manifest, "w") as f:
            f.write("Main-Class: " + args["exe_class"] + '\n')
        utils.call("jar", "ufm", dest, manifest)
        