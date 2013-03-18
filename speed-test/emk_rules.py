num = 0

if "num" in emk.options:
    num = int(emk.options["num"])

for i in xrange(num):
    emk.subdir("d_%d" % (i))
