emk.module_paths.append(emk.abspath("modules"))
c, revision = emk.module("c", "revision")
emk.depend("$:build:$/revision" + c.obj_ext, "revision.h")
