import sys

c, link, java = emk.module("c", "link", "java")

c.flags.append("-fpic")
if sys.platform == "darwin":
    c.include_dirs.append("/System/Library/Frameworks/JavaVM.framework/Headers")
else:
    # assume Linux for now
    # NOTE: you may need to change these include dirs depending on where your JDK is installed!
    c.include_dirs += ["/usr/lib/jvm/java-6-openjdk/include", "/usr/lib/jvm/java-6-openjdk/include/linux"]

link.make_static_lib = False
link.make_shared_lib = True
link.shared_libname = "tutorial.jnilib"
link.projdirs.append("library")

java.resources.append(("$:build:$/tutorial.jnilib", "jnilibs/tutorial.jnilib"))
