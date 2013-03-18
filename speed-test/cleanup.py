#!/usr/bin/env python

import os
import shutil

try:
    os.remove("generic.h")
except OSError:
    pass
    
try:
    os.remove("results.txt")
except OSError:
    pass
    
for i in xrange(64):
    d = "d_%d" % (i)
    try:
        shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
