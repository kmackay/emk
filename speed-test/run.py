#!/usr/bin/env python

from __future__ import print_function

import os
import subprocess
import re

time_regex = re.compile(r'real\s+(\d+\.\d+)')

tests = [1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 20, 24, 32, 48, 64]

try:
    os.remove("results.txt")
except OSError:
    pass

for num in tests:
    proc = subprocess.Popen(["../emk", "num=%d" % (num), "clean"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.communicate()
    
    proc = subprocess.Popen(["time", "-p", "../emk", "num=%d" % (num), "log=critical"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc_stdout, proc_stderr = proc.communicate()
    
    match = time_regex.search(proc_stderr)
    if match:
        with open("results.txt", "a+") as f:
            f.write("%d %s\n" % (num * 1000, match.group(1)))
    else:
        print("Error running test for %d" % (num * 1000))
        break
    print("Finished %d file compile test")
    