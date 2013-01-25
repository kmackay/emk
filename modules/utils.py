import os
import errno
import subprocess
import traceback
import shutil
import logging
import glob
import filecmp

log = logging.getLogger("emk.utils")

class Module(object):
    def __init__(self, scope):
        self._clean_rules = 0
    
    def new_scope(self, scope):
        return Module(scope)
        
    def flatten_flags(self, flags):
        result = []
        for flag in flags:
            try:
                flag.startswith('a')
                result.append(flag)
            except AttributeError:
                result.extend(self.flatten_flags(flag))
        return result

    def unique_list(self, orig):
        result = []
        seen = set()
        for item in orig:
            if not item in seen:
                seen.add(item)
                result.append(item)
        return result
    
    def rm_list(self, thelist, item):
        try:
            thelist.remove(item)
        except ValueError:
            pass

    def mkdirs(self, path):
        try:
            os.makedirs(path)
        except OSError as e:
            if e.errno == errno.EEXIST and os.path.isdir(path):
                pass
            else:
                raise

    def rm(self, path, print_msg=False):
        if print_msg:
            if os.path.isabs(path):
                log.info("Removing %s", path)
            else:
                log.info("Removing %s", os.path.realpath(os.path.join(os.getcwd(), path)))
        try:
            os.remove(path)
        except OSError:
            shutil.rmtree(path, ignore_errors=True)
    
    class cd(object):
        def __init__(self, path):
            self.dest = path

        def __enter__(self):
            self.orig = os.getcwd()
            os.chdir(self.dest)

        def __exit__(self, *args):
            os.chdir(self.orig)

    def call(self, *args, **kwargs):
        print_call = True
        print_stdout = False
        print_stderr = "nonzero"
        exit_on_nonzero_return = True
        cwd = None
        env = None

        if "print_call" in kwargs and not kwargs["print_call"]:
            print_call = False
        if "print_stdout" in kwargs and kwargs["print_stdout"]:
            print_stdout = True
        if "print_stderr" in kwargs:
            print_stderr = kwargs["print_stderr"]
        if "noexit" in kwargs and kwargs["noexit"]:
            exit_on_nonzero_return = False
        if "cwd" in kwargs:
            cwd = kwargs["cwd"]
        if "env" in kwargs:
            env = kwargs["env"]

        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env)
        proc_stdout, proc_stderr = proc.communicate()
        strings = []
        if print_call:
            strings.append(' '.join(args))
        if print_stdout and proc_stdout:
            strings.append(emk.style_tag('u') + "Subprocess stdout:" + emk.end_style())
            strings.append(emk.style_tag('stdout') + proc_stdout + emk.end_style())
        if (print_stderr == True or (print_stderr == "nonzero" and proc.returncode != 0)) and proc_stderr:
            strings.append(emk.style_tag('u') + "Subprocess stderr:" + emk.end_style())
            strings.append(emk.style_tag('stderr') + proc_stderr + emk.end_style())
        if strings:
            emk.log_print('\n'.join(strings))
        if exit_on_nonzero_return and proc.returncode != 0:
            stack = emk.fix_stack(traceback.extract_stack()[:-1])
            if emk.options["log"] == "debug" and emk.current_rule:
                stack.append("Rule definition:")
                stack.extend(["    " + emk.style_tag('rule_stack') + line + emk.end_style() for line in emk.current_rule.stack])
            raise emk.BuildError("In directory %s:\nSubprocess '%s' returned %s" % (emk.scope_dir, ' '.join(args), proc.returncode), stack)
        return (proc_stdout, proc_stderr, proc.returncode)

    def mark_exists_rule(self, produces, requires):
        emk.rule(produces, requires, self.mark_exists, threadsafe=True, ex_safe=True)
        
    def mark_exists(self, produces, requires, args):
        emk.mark_exists(*produces)
    
    def copy_rule(self, source, dest):
        emk.rule([dest], [source, emk.ALWAYS_BUILD], self.copy_file, threadsafe=True, ex_safe=True)
    
    def copy_file(self, produces, requires, args):
        dest = produces[0]
        src = requires[0]
        
        try:
            if(os.path.isfile(dest) and filecmp.cmp(dest, src, shallow=False)):
                emk.mark_untouched(*produces)
                return
                
            emk.log.info("Copying %s to %s" % (src, dest))
            destdir = os.path.dirname(dest)
            self.mkdirs(destdir)
            shutil.copy2(src, dest)
        except:
            self.rm(dest)
            raise

    def clean_rule(self, *patterns):
        target = "__clean_rule_%d__" % (self._clean_rules)
        self._clean_rules += 1
        emk.rule([target], [emk.ALWAYS_BUILD], self.do_cleanup, args=patterns)
        emk.attach("clean", target)
    
    def do_cleanup(self, produces, requires, args):
        patterns = args
        for pattern in patterns:
            for f in glob.glob(pattern):
                self.rm(f, print_msg=True)
        emk.mark_exists(*produces)
