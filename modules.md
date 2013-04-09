---
title: emk Modules
layout: default
---

emk Modules
===========

emk provides various modules for common build tasks. Here is a list of the currently provided modules; the heading of each section
is a link to that module's detailed documentation.

### [Utils Module](modules/utils.html)
The Utils module contains various utility functions and build rules such as creating directories, removing or copying files, or setting up patterns for cleaning.

### [C/C++ Module](modules/c.html)
The C module provides automatic detection and compilation of C and C++ source code. It leverages the Link module for creating libraries and executables.

### [Link Module](modules/link.html)
The Link module can build static and dynamic libraries, and links executables. It provides automatic detection of executables
(files that contain a `main()` functions) from source code or from compiled object files.

### [Java Module](modules/java.html)
The Java module provides automatic detection and compilation of Java code, as well as the automatic creation of a jar file for each directory. It will also autodetect
classes that contain a `main()` method; for each class with a `main()` method, the Java module will create an executable jar.
