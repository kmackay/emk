c = emk.module("c")
c.include_dirs.append("$:proj:$")
c.defines["DEFINED_VALUE"] = 10
