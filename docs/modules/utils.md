Utils Module
============

This module provides various utility methods. It does not have any configurable properties.

Classes
-------

#### `utils.cd` Context Manager
This is a simple context manager for changing to a directory, and always returning to the original directory.

Usage:
```python
with utils.cd("some/path"):
    # do stuff; in here the working directory will be set to some/path
# the working directory will always be returned to its original state.
```

Methods
-------

#### `utils.flatten(args)`
Convert a string, a list of strings, or a list of lists of ... of strings into a single list of strings. Any iterable counts
as a list (but a real list is returned). Returns a flattened version of the input, which is always a list containing only strings.

Arguments:
 * **args**: The string or list to flatten.

#### `utils.unique_list(orig)`
Create a new list from the input list, with duplicate items removed. Order is preserved. The list items must be hashable.
Returns a copy of the original list with duplicate items removed.

Arguments:
 * **orig**: The original list. This list is not modified.

#### `utils.rm_list(thelist, item)`
Remove an item from a list, if it is present. It is not an error if the item is not in the list.

Arguments:
 * **thelist**: The list to remove the item from. This list is modified.
 * **item**: The item to remove if it is present in the list.

#### `utils.mkdirs(path)`
Create all nonexistent directories in a path. It is not an error if the path already exists and is a directory.
If the path already exists and is not a directory, an OSError will be raised.

Arguments:
 * **path**: The absolute or relative path to create directories for.

#### `utils.rm(path, print_msg=False)`
Delete a file or directory tree. It is not an error if the file or directory does not exist.

Arguments:
 * **path**: The file or directory tree to delete.
 * **print_msg**: If True, a log message is printed about the removal. The default value is False.

#### `utils.symlink(source, link_name)`
Create a symbolic link pointing to source named link_name.  If symbolic links are not supported, then the source will be copied to link_name.

Arguments:
  * **source**: The file or directory that the link is to point to.
  * **link_name**: The name of the link to create.

#### `utils.call(*args, **kwargs)`
Call a subprocess. Returns a tuple (stdout, stderr, exit code).

The subprocess will run until it exits (normally or otherwise). The stdout, stderr, and exit code of the
subprocess are returned if the process exits normally. Otherwise, the default behaviour is to raise a build error,
but this can be suppressed using the "noexit" keyword argument.

Arguments:
 * All non-keyword arguments are used to create the subprocess (they are passed to subprocess.Popen()).

Keyword arguments:
 * **cwd**: Set the working directory that the subprocess will run in. By default, the subprocess will run
            in the working directory of the current process.
 * **env**: Set the environment for the calling process. Passed directly to subprocess.Popen().
            The default value is None (ie, the current process environment will be used).
 * **noexit**: If True, a non-zero exit code will not raise an error; instead, the normal (stdout, stderr, code) will
               be returned. The default value is False.
 * **print_call**: If True, the subprocess call will be logged. The default value is True.
 * **print_stdout**: If True, the stdout of the subprocess will be logged (after the subprocess exits). Otherwise,
                     the subprocess stdout will not be logged. The default value is False.
 * **print_stderr**: If True, the stderr of the subprocess will be logged (after the subprocess exits). If "nonzero",
                     the subprocess stderr will be logged only if the subprocess exits abnormally (with a nonzero exit code).
                     If False, the subprocess stderr will not be logged. The default value is "nonzero".
 * **error_stream**: Controls which output stream is logged as an error. Can be set to "none", "stdout", "stderr", or "both".
			         The default value is "stderr".

#### `utils.mark_virtual_rule(produces, requires)`
Define an emk rule to mark the productions as virtual.

Arguments:
 * **produces**: The paths to mark as virtual when the rule is executed.
 * **requires**: The dependencies of the rule.

#### `utils.copy_rule(source, dest)`
Define an emk rule to copy a file. The file will only be copied if the source differs from the destination (or the destination does not yet exist).
Directories containing the destination that do not exist will be created.

Arguments:
 * **source**: The source file to copy; it treated as an emk dependency (so if there is a rule that produces the source,
               that rule will be executed before the copy rule is).
 * **dest**: The path to copy the file to; must include the destination file name (ie not just the directory).

#### `utils.clean_rule(*patterns)`
Add patterns for files to remove when "emk clean" is called. This attaches a rule to the "clean" target that will remove files matching the given patterns.
Returns the product path generated by the new rule (in case you want to depend on it or whatever).

Arguments:
 * **patterns**: The patterns for files to remove when cleaning, in glob format.

#### `utils.get_environment_from_batch_command(env_cmd, initial)`
Execute a Windows batch or command file and retrieve the resulting environment.

Arguments:
 * **env_cmd**: The command to run.
 * **initial**: The initial environment dictionary, if any.
