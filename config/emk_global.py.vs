c, link = emk.module("c", "link")
c.compiler = c.MsvcCompiler()
link.linker = link.MsvcLinker()
