#!/usr/bin/env python

import os
import shutil

generic_header = """
#ifndef GENERIC_HEADER
#define GENERIC_HEADER
int generic_func(void);
#endif
"""

shared_header = """
#ifndef SHARED_HEADER_%(num)s
#define SHARED_HEADER_%(num)s
int shared_%(num)s(void);
#endif
"""

shared_impl = """
#include "shared_%(num)s.h"
int shared_%(num)s(void)
{
    return %(num)s;
}
"""

single_header = """
#ifndef SINGLE_HEADER_%(num)s
#define SINGLE_HEADER_%(num)s
int single_%(num)s(void);
#endif
"""

single_impl = """
#include "../generic.h"
#include "shared_%(shared_num)s.h"
#include "single_%(num)s.h"
int single_%(num)s(void)
{
    return %(num)s + shared_%(shared_num)s() + generic_func();
}
"""

class cd(object):
    def __init__(self, path):
        self.dest = path

    def __enter__(self):
        self.orig = os.getcwd()
        os.chdir(self.dest)

    def __exit__(self, *args):
        os.chdir(self.orig)

def make_shared(num):
    with open("shared_%d.h" % (num), "wb") as f:
        f.write(shared_header % {"num": num})
    with open("shared_%d.c" % (num), "wb") as f:
        f.write(shared_impl % {"num": num})

def make_single(num):
    with open("single_%d.h" % (num), "wb") as f:
        f.write(single_header % {"num": num})
    with open("single_%d.c" % (num), "wb") as f:
        f.write(single_impl % {"num": num, "shared_num": (num // 100) * 100})

def make_test_files(dest):
    with cd(dest):
        for i in xrange(1000):
            if i % 100 == 0:
                make_shared(i)
            make_single(i)

with open("generic.h", "wb") as f:
    f.write(generic_header)

for i in xrange(64):
    d = "d_%d" % (i)
    try:
        os.mkdir(d)
    except OSError:
        pass

    shutil.copyfile("emk_rules.py.template", os.path.join(d, "emk_rules.py"))
    make_test_files(d)
    