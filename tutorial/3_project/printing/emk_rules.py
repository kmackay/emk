c, link = emk.module("c", "link")
c.defines["DEFINED_VALUE"] = 999
link.projdirs += ["math"]
