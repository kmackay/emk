import os
import errno
import subprocess
import traceback

class Module(object):
    def __init__(self, scope):
        pass
    
    def new_scope(self, scope):
        return self
        
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

    def mkdirs(self, path):
        try:
            os.makedirs(path)
        except OSError as e:
            if e.errno == errno.EEXIST and os.path.isdir(path):
                pass
            else:
                raise

    def rm(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def call(self, *args, **kwargs):
        print_call = True
        print_stdout = False
        print_stderr = "nonzero"
        exit_on_nonzero_return = True
        cwd = None

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

        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd)
        proc_stdout, proc_stderr = proc.communicate()
        strings = []
        if print_call:
            strings.append(' '.join(args))
        if print_stdout and proc_stdout:
            strings.append("emk: Subprocess stdout:")
            strings.append(proc_stdout)
        if (print_stderr == True or (print_stderr == "nonzero" and proc.returncode != 0)) and proc_stderr:
            strings.append("emk: Subprocess stderr:")
            strings.append(proc_stderr)
        if strings:
            emk.log_print('\n'.join(strings))
        if exit_on_nonzero_return and proc.returncode != 0:
            stack = emk.fix_stack(traceback.extract_stack()[:-1])
            raise emk.BuildError("Subprocess '%s' returned %s" % (' '.join(args), proc.returncode), stack)
        return (proc_stdout, proc_stderr, proc.returncode)

    def mark_exists(self, produces, requires, args):
        emk.mark_exists(*produces)
