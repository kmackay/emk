---
title: emk Tutorial
layout: default
---

emk Tutorial
============

This tutorial gives examples of basic usage of emk and goes over some of the features.
See the [Manual](manual.html) for full documentation of emk's features.

First, clone the emk repository in the location of your choice: `git clone https://github.com/kmackay/emk.git`. This will create
the root emk directory. All of the tutorial code is available in the emk repository, in the `tutorial` directory.

Before starting the tutorial, you should install emk (by running `sudo ./setup.py install` (\*nix) or `python setup.py install` (Windows)) in the root
emk directory) so that you can run emk simply by calling `emk` (rather than needing to use an absolute or relative path).
You also need gcc and binutils installed (or you can use Visual Studio on Windows; see below), and a JDK if you want to run the Java examples.

#### Windows

The command-line commands and sample output below are based on Linux/OSX. On Windows, the file extensions will not match (for example, executables
fill end in `.exe` instead of nothing). You will also need to make sure to use the correct commands for the Windows cmd.exe.

#### Visual Studio

To compile and link using Visual Studio instead of gcc, rename/copy the provided `emk_global.py.vs` file
in the `<emk>/config` directory to `emk_global.py`. After this is done, `emk_global.py` should contain the following:
{% highlight python %}
c, link = emk.module("c", "link")
c.compiler = c.MsvcCompiler()
link.linker = link.MsvcLinker()
{% endhighlight %}

1\. Basics
----------

In this section, we will create a simple C function in one file, and an executable that calls that function. First we create a new directory for this section:
{% highlight bash %}
xxxx:tutorial kmackay$ mkdir 1_basics
xxxx:tutorial kmackay$ cd 1_basics
{% endhighlight %}

#### Create the emk rules file
Create the `emk_rules.py` file in that directory. Here are the contents:
{% highlight python %}
emk.module("c")
{% endhighlight %}

This sets up emk to automatically detect and build C and C++ source files, and link them into static libraries or executables (depending on
whether or not the source file defines a `main()` function).

#### C files
Create a header file `print_function.h` declaring a `do_print()` function:
{% highlight c %}
#ifndef PRINT_FUNCTION_H
#define PRINT_FUNCTION_H

void do_print(void);

#endif
{% endhighlight %}

And create `print_function.c` to actually implement `do_print()`:
{% highlight c %}
#include "print_function.h"

#include <stdio.h>

void do_print(void)
{
    printf("In the emk tutorial, part 1\n");
}
{% endhighlight %}

Now create a C program `print.c` that will call `do_print()`:
{% highlight c %}
#include "print_function.h"

int main()
{
    do_print();
    return 0;
}
{% endhighlight %}

#### Building

Run `emk` in the directory; remember that all build output will be placed in the `__build__` directory (which is created if needed). emk's c module
will compile and link the C files into a static library `lib1_basics.a` (containing the compiled `print_function.c`) and an executable `print`
(from `print.c`). You can now run the executable:
{% highlight bash %}
xxxx:1_basics kmackay$ __build__/print 
In the emk tutorial, part 1
{% endhighlight %}

2\. Java
--------

In this section, we will do the same thing as in section 1, but for Java. First we create a new directory for this section:
{% highlight bash %}
xxxx:tutorial kmackay$ mkdir 2_java
xxxx:tutorial kmackay$ cd 2_java
{% endhighlight %}

#### Create the emk rules file
Create the `emk_rules.py` file in that directory. Here are the contents:
{% highlight python %}
emk.module("java")
{% endhighlight %}

This sets up emk to automatically detect and build Java source files, and link them into a jar file. An executable jar file will be created for
any Java class that contains a `main()` method.

#### Java files
Create a java file `PrintFunction.java` containing a `do_print()` method:
{% highlight java %}
class PrintFunction
{
    void do_print()
    {
        System.out.println("In the emk tutorial, part 2");
    }
}
{% endhighlight %}

Now create a Java program `print.java` that will call `do_print()`:
{% highlight java %}
class print
{
    public static void main(String[] argv)
    {
        new PrintFunction().do_print();
    }
}
{% endhighlight %}

#### Building

Run `emk` in the directory; remember that all build output will be placed in the `__build__` directory (which is created if needed). emk's java module will
compile the java files and put them into a `2_java.jar` file; it will also create an executable `print.jar` that will call the `main()` method of the `print`
class. You can now run `print.jar`:
{% highlight bash %}
xxxx:2_java kmackay$ java -jar __build__/print.jar
In the emk tutorial, part 2
{% endhighlight %}

3\. Project
-----------

In this section, we will create a couple of C libraries to show off the transitive linking abilities of emk. We will also create an `emk_project.py` file
to demonstrate how it can make managing larger projects simpler, and to show off the hierarchical configuration. First we create a new directory for this section:
{% highlight bash %}
xxxx:tutorial kmackay$ mkdir 3_project
xxxx:tutorial kmackay$ cd 3_project
{% endhighlight %}

### Create the project-level emk files

Create an `emk_project.py` file containing the following:
{% highlight python %}
c = emk.module("c")
c.include_dirs.append("$:proj:$")
c.defines["DEFINED_VALUE"] = 10
{% endhighlight %}

This sets the project directory for emk in any subdirectories to the current directory. The project directory is available via the `emk.proj_dir` property,
or you can use the `$:proj:$` placeholder in strings passed to emk. We add the project directory as an include directory for the c module; this allows C code
to #include headers relative to the project directory rather than relative to the directory the C code is in.

We also set up a C DEFINED_VALUE macro; this will be defined as 10 in all subdirectories of the project directory, unless the value is overridden by
an emk_rules.py file (or emk_subproj.py).

Next we create an `emk_rules.py` file in the project directory. This is not required but is useful if you want to build/clean your project from the project directory.
{% highlight python %}
emk.subdir("math", "printing", "exes")
{% endhighlight %}

This file just tells emk to recurse into the 3 subdirectories that we will create, and to clean in those directories if `emk clean` is run in the project directory.

### Create the subdirectories

#### math
The first library directory will be called `math`, and will contain the following files:

`emk_rules.py`:
{% highlight python %}
emk.module("c")
{% endhighlight %}

`math.h`:
{% highlight c %}
#ifndef MATH_H
#define MATH_H

int sum(int a, int b);

#endif
{% endhighlight %}

`math.c`:
{% highlight c %}
#include "math.h"

int sum(int a, int b)
{
    return a + b;
}
{% endhighlight %}

#### printing
The second library directory will be called `printing`, and will contain the following files:

`emk_rules.py`:
{% highlight python %}
c, link = emk.module("c", "link")
c.defines["DEFINED_VALUE"] = 999
link.projdirs += ["math"]
{% endhighlight %}

Note that we override the DEFINED_VALUE macro in this directory. We also use `link.projdirs` to tell the link module that the code in this
directory depends on code in the `math` directory; the paths in `link.projdirs` are relative to the project directory. You could also use
`link.depdirs += ["../math"]` to achieve the same result (depdirs are absolute, or relative to the current directory).

`printing.h`:
{% highlight c %}
#ifndef PRINTING_H
#define PRINTING_H

void print_sum(int a, int b);

#endif
{% endhighlight %}

`printing.c`:
{% highlight c %}
#include "printing.h"
#include "math/math.h"

#include <stdio.h>

void print_sum(int a, int b)
{
    printf("%d + %d = %d\n", a, b, sum(a, b));
    printf("The defined value in printing.c is %d\n", DEFINED_VALUE);
}
{% endhighlight %}

#### exes
The final directory will be called `exes`, and will contain the example executable:
`emk_rules.py`:
{% highlight python %}
c, link = emk.module("c", "link")
link.projdirs += ["printing"]
{% endhighlight %}

This rules file tells the link module that the code in this directory depends on the code in the `printing` directory. Note that although the executable
indirectly depends on the `math` directory, we do not need to add that as a dependency; emk will pick that dependency up automatically from the
`printing` directory. This is an example of the link module's transitive properties.

`test.c`:
{% highlight c %}
#include "printing/printing.h"

#include <stdio.h>

int main()
{
    print_sum(1234, 5678);
    printf("In the emk tutorial, part 3. The defined value in test.c is %d\n", DEFINED_VALUE);
    return 0;
}
{% endhighlight %}

This is the test executable that we will run to see the resulting output.

### Building

We can now build in the `exes` directory:
{% highlight bash %}
xxxx:exes kmackay$ emk
{% endhighlight %}

This will build all the necessary files and create a `__build__/test` executable. We can now run that executable:
{% highlight bash %}
xxxx:exes kmackay$ __build__/test 
1234 + 5678 = 6912
The defined value in printing.c is 999
In the emk tutorial, part 3. The defined value in test.c is 10
{% endhighlight %}

You can see that the DEFINED_VALUE was 10 in `test.c` (inherited from `emk_project.py`), but in `printing.c`, the value was 999 since we overrode the value in
the `emk_rules.py` file for that directory.

4\. Rules
---------

In this section we will demonstrate the creation of a new emk rule. The rule will generate a header file containing information about the current git revision and URL.
We will create a test program that depends on that header file; this will show how to add dependencies to a target. First we create a new directory for this section:
{% highlight bash %}
xxxx:tutorial kmackay$ mkdir 4_rules
xxxx:tutorial kmackay$ cd 4_rules
{% endhighlight %}

#### The rules file

The `emk_rules.py` file will contain a lot of code to get the git revision and URL, and to generate the header file. Here is the code:
{% highlight python %}
import os

c, utils = emk.module("c", "utils")

def get_git_revision(in_dir):
    rev, err, code = utils.call("git", "rev-parse", "--short", "HEAD", print_call=False, cwd=in_dir)
    return rev.strip()

def get_git_branch(in_dir):
    branch = ""
    out, err, code = utils.call("git", "branch", print_call=False, print_stderr=False, cwd=in_dir)
    lines = out.splitlines()
    for line in lines:
        if line.startswith("* "):
            branch = line[2:]
            break
    return branch

def get_git_url(in_dir):
    out, err, code = utils.call("git", "remote", print_call=False, cwd=in_dir)
    urls = []
    
    for repo in out.split():
        info, err, code = utils.call("git", "remote", "show", "-n", repo, print_call=False, cwd=in_dir)
        lines = info.splitlines()
        for line in lines:
            if line.startswith("  Fetch URL: "):
                url = line[13:]
                if url:
                    urls.append(url)
    return ', '.join(urls)

@emk.make_rule("revision.h", emk.ALWAYS_BUILD, cwd_safe=True)
def generate_revision_header(produces, requires):
    cache = emk.rule_cache(produces[0])  
    current_revision = "%s (%s)" % (get_git_revision(emk.scope_dir), get_git_branch(emk.scope_dir))
    if "last_revision" in cache and cache["last_revision"] == current_revision and os.path.isfile(produces[0]):
        return
    cache["last_revision"] = current_revision

    template = """
#ifndef GENERATED_REVISION_H
#define GENERATED_REVISION_H

#define REVISION "%(revision)s"
#define URL "%(url)s"

#endif
"""
    with open(produces[0], "w") as f:
        f.write(template % {"revision": current_revision, "url": get_git_url(emk.scope_dir)})

emk.depend("$:build:$/revision" + c.obj_ext, "revision.h")
utils.clean_rule("revision.h")
{% endhighlight %}

First, we import the `os` Python module since we use it later. Then we load the `c` and `utils` emk modules.

We then define the `get_git_revision`, `get_git_branch`, and `get_git_url` functions to get imformation about the git repository; these use the `utils.call` method
supplied by the emk `utils` module to call various git utilities and get the output, which is then parsed to get the desired information.

Next, we create a new emk rule to generate a `revision.h` file. Since we are only creating a single rule, we use the `@emk.make_rule` decorator.
We make the rule always be built by including `emk.ALWAYS_BUILD` in the requires list; this is because we want the rule to check if the git revision has
changed (if is has, the rule needs to update the generated header file). We declare the rule as cwd_safe since it does not depend on the current working directory.
Note that the relative path "revision.h" is converted to an absolute path before it is passed to the rule function.

The rule function gets the emk cache for the generated header file (using `emk.rule_cache()`). It then gets the current git revision,
and compares it against the cached value. If there is a cached revision value, and it is the same as the current git revision, then
the generated header file does not need to be changed; therefore, if the header file already exists, the rule function just returns
without changing anything. Otherwise, it stores the new revision value in the emk cache, and then (re)generates the header file.

We need to tell emk that the C file (`revision.c`) that uses the generated header file has a dependency on the header file. This is required because emk
must generate the header file before the C file can be compiled the first time; it is not required for normal header files since they already exist.
To add the dependency, we call `emk.depend("$:build:$/revision" + c.obj_ext, "revision.h")`; this tells emk that before it can compile the C file into an
object file, it must build `revision.h`. Note that it would be possible to build a module (or modify the existing c module) to examine the C source to
automatically determine header file dependencies so that this manual process is not required.

Finally we call `utils.clean_rule("revision.h")` so that `revision.h` will be deleted when `emk clean` is run.

Note that ordinarily, emk would not build `revision.h` unless told to; however, since it is a dependency of something that is autobuilt
(by the link module), it will be built automatically if no targets are explicitly specified. To declare that something should be built
automatically when no targets are specified, use `emk.autobuild()` (for example, in this case you would use `emk.autobuild("revision.h")`).
See the [Manual](manual.html#loading-sequence) for more information about emk's loading process and which targets will be built.

#### C file

We just have a simple C program `revision.c` that prints out the git information from the generated header file:
{% highlight c %}
#include "revision.h"

#include <stdio.h>

int main()
{
    printf("In the emk tutorial, part 4: Revision %s from %s\n", REVISION, URL);
    return 0;
}
{% endhighlight %}

#### Building

Run `emk` in the directory; remember that all build output will be placed in the `__build__` directory (which is created if needed). You can now run the
`revision` executable:
{% highlight bash %}
xxxx:4_rules kmackay$ __build__/revision 
In the emk tutorial, part 4: Revision b2cf3e6 (master) from ssh://git@github.com/kmackay/emk.git
{% endhighlight %}

5\. Modules
-----------

In the previous section, we created some functions to get information about the git repository, and created a rule to generate a header file
based on that information. However, it would be nice to abstract all of that code out into an emk module; then it would be easily available
for reuse in any `emk_rules.py` file. This section will explain how to make a module. First we create a new directory for this section:
{% highlight bash %}
xxxx:tutorial kmackay$ mkdir 5_modules
xxxx:tutorial kmackay$ cd 5_modules
{% endhighlight %}

#### Creating the module

First we create a subdirectory to put the module in (this is not required, but is a good way to organize things):
{% highlight bash %}
xxxx:tutorial kmackay$ mkdir modules
xxxx:tutorial kmackay$ cd modules
{% endhighlight %}

Then we create the module file `revision.py`, which contains the following code:
{% highlight python %}
import os
import logging

utils = emk.module("utils")
log = logging.getLogger("emk.revision")

def get_git_revision(in_dir):
    rev, err, code = utils.call("git", "rev-parse", "--short", "HEAD", print_call=False, cwd=in_dir)
    return rev.strip()

def get_git_branch(in_dir):
    branch = ""
    out, err, code = utils.call("git", "branch", print_call=False, print_stderr=False, cwd=in_dir)
    lines = out.splitlines()
    for line in lines:
        if line.startswith("* "):
            branch = line[2:]
            break
    return branch

def get_git_url(in_dir):
    out, err, code = utils.call("git", "remote", print_call=False, cwd=in_dir)
    urls = []
    
    for repo in out.split():
        info, err, code = utils.call("git", "remote", "show", "-n", repo, print_call=False, cwd=in_dir)
        lines = info.splitlines()
        for line in lines:
            if line.startswith("  Fetch URL: "):
                url = line[13:]
                if url:
                    urls.append(url)
    return ', '.join(urls)

def generate_revision_header(produces, requires):
    cache = emk.rule_cache(produces[0])  
    current_revision = "%s (%s)" % (get_git_revision(emk.scope_dir), get_git_branch(emk.scope_dir))
    if "last_revision" in cache and cache["last_revision"] == current_revision and os.path.isfile(produces[0]):
        log.info("Revision has not changed; not updating %s", produces[0])
        return
    cache["last_revision"] = current_revision

    template = """
#ifndef GENERATED_REVISION_H
#define GENERATED_REVISION_H

#define REVISION "%(revision)s"
#define URL "%(url)s"

#endif
"""
    log.info("Generating revision file %s", produces[0])
    with open(produces[0], "w") as f:
        f.write(template % {"revision": current_revision, "url": get_git_url(emk.scope_dir)})

class Module(object):
    def __init__(self, scope, parent=None):
        if parent:
            self.output_name = parent.output_name
        else:
            self.output_name = "revision.h"
    
    def new_scope(self, scope):
        return Module(scope, parent=self)
    
    def post_rules(self):
        utils.clean_rule(self.output_name)
        if not emk.cleaning:
            emk.rule(generate_revision_header, self.output_name, emk.ALWAYS_BUILD, cwd_safe=True)
{% endhighlight %}

First, the module file imports some required modules and sets up logging for the `revision` module (this is not required, but is nice to have).
Then we have the same functions (from section 4 of the tutorial) to get the git information, as well as the rule function.

Then, we have the actual module definition. This is just a class named Module, which has two requirements:
 * The constructor must take at least one argument, which is the scope that the module is being created in.
 * The module instance must provide a `new_scope()` method, which must take one argument which is the new scope that is being entered.

In this example we set up the constructor and `new_scope()` method as you would do for inheriting configuration values. The only configuration
value that can be inherited is the name of the file to output.

Optionally the module instance may also have `load_global()`, `post_global()`, `load_project()`, `post_project()`, `load_subproj()`, `post_subproj()`,
`load_rules()`, or `post_rules()` methods. The `load_<scope>()` method will be called called when the module is loaded into the named scope; the `post_<scope>()`
method will be called after the scope file has been handled (eg, `post_rules()` will be called after the `emk_rules.py` file has been handled).
In this example, we define a `post_rules()` method to set up the rule to generate the code (if emk is not cleaning) and to make the generated code be deleted
when cleaning.

#### The rules file

The C code will be the same as for the previous section; we just need to update the `emk_rules.py` file to load the new module and use it.
Here is the contents of `emk_rules.py` (back in the `5_modules` directory):
{% highlight python %}
emk.module_paths.append(emk.abspath("modules"))
c, revision = emk.module("c", "revision")
emk.depend("$:build:$/revision" + c.obj_ext, "revision.h")
{% endhighlight %}

First we tell emk that it can also load modules from the `modules` directory. We then load the builtin `c` module and the `revision` module that we just created.
Then we add the dependency on `revision.h` to the object file as before.

Here is the `revision.c` file, which is the same as in section 4:
{% highlight c %}
#include "revision.h"

#include <stdio.h>

int main()
{
    printf("In the emk tutorial, part 5: Revision %s from %s\n", REVISION, URL);
    return 0;
}
{% endhighlight %}

#### Building

Run `emk` in the `5_modules` directory; remember that all build output will be placed in the `__build__` directory (which is created if needed). You can now run the
`revision` executable:
{% highlight bash %}
xxxx:5_modules kmackay$ __build__/revision 
In the emk tutorial, part 5: Revision 6dc0380 (master) from ssh://git@github.com/kmackay/emk.git
{% endhighlight %}
