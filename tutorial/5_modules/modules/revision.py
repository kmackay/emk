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
