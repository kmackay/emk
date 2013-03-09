#!/usr/bin/env python

from __future__ import print_function

import os
import sys
import emk

def usage():
    print("Usage (as root/sudo): 'setup.py install' or 'setup.py uninstall'")
    sys.exit(1)

def install():
    emk_dir, tail = os.path.split(emk._module_path)
    bin_path = os.path.join(emk_dir, "emk")
    if os.path.exists("/usr/bin/emk"):
        print("/usr/bin/emk already exists; will not overwrite.")
    else:
        os.symlink(os.path.join(emk_dir, "emk"), "/usr/bin/emk")
        print("Created symlink /usr/bin/emk -> %s" % (bin_path))

def uninstall():
    emk_dir, tail = os.path.split(emk._module_path)
    bin_path = os.path.join(emk_dir, "emk")
    try:
        if os.readlink("/usr/bin/emk") == bin_path:
            os.remove("/usr/bin/emk")
            print("Removed /usr/bin/emk")
            return
    except OSError:
        pass
    print("/usr/bin/emk is not a symlink, or does not point to this instance of emk (%s)" % (bin_path))

if len(sys.argv) != 2:
    usage()

if sys.argv[1] == "install":
    install()
elif sys.argv[1] == "uninstall":
    uninstall()
else:
    usage()
