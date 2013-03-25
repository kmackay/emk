#!/usr/bin/env python

from __future__ import print_function

import os
import sys
import emk

if sys.platform == "win32":
    import _winreg as winreg
    from ctypes import windll

    class EnvPath(object):
        def __init__(self):
            regpath = 'SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment'
            self.SendMessage = windll.user32.SendMessageW
            self.HWND_BROADCAST = 0xFFFF
            self.WM_SETTINGCHANGE = 0x001A
            self.reg = winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE)
            self.key = winreg.OpenKey(self.reg, regpath, 0, winreg.KEY_ALL_ACCESS)

        def _query(self):
            value, type_id = winreg.QueryValueEx(self.key, 'PATH')
            return value.split(';')

        def add(self, path):
            items = self._query()
            if path not in items:
                items.append(path)
                value = ';'.join(items)
                winreg.SetValueEx(self.key, 'PATH', 0, winreg.REG_EXPAND_SZ, value)
                self.SendMessage(self.HWND_BROADCAST, self.WM_SETTINGCHANGE, 0, 'Environment')
                return True
            return False

        def remove(self, path):
            items = self._query()
            if path in items:
                items.remove(path)
                value = ';'.join(items)
                winreg.SetValueEx(self.key, 'PATH', 0, winreg.REG_EXPAND_SZ, value)
                return True
            return False

def usage():
    print("Usage (as root/sudo/admin): 'setup.py install' or 'setup.py uninstall'")
    sys.exit(1)

def install():
    emk_dir, tail = os.path.split(emk._module_path)

    if sys.platform == "win32":
        path = EnvPath()
        if path.add(emk_dir):
            print("emk directory added to PATH")
        else:
            print("emk directory already in PATH")
    else:
        bin_path = os.path.join(emk_dir, "emk")
        if os.path.exists("/usr/bin/emk"):
            print("/usr/bin/emk already exists; will not overwrite.")
        else:
            os.symlink(os.path.join(emk_dir, "emk"), "/usr/bin/emk")
            print("Created symlink /usr/bin/emk -> %s" % (bin_path))

def uninstall():
    emk_dir, tail = os.path.split(emk._module_path)

    if sys.platform == "win32":
        path = EnvPath()
        if path.remove(emk_dir):
            print("emk directory removed from PATH")
        else:
            print("emk directory not found in PATH")
    else:
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
