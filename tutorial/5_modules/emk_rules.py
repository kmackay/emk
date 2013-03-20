emk.module_paths.append(emk.abspath("modules"))
c, revision = emk.module("c", "revision")
emk.depend("$:build:$/revision.o", "revision.h")
