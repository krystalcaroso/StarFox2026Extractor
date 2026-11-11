"""Resolve hashed VZIP names to paths.  name = '%x' % fnv1a64(path_without_leading_slash)."""
import sys, os, re, pickle, json
sys.path.insert(0, os.path.dirname(__file__))
from vzip import VZip
M = (1 << 64) - 1
def fnv1a64(b):
    h = 0xcbf29ce484222325
    for c in b: h = ((h ^ c) * 0x100000001b3) & M
    return h
def key(p): return '%x' % fnv1a64(p.encode('utf-8', 'surrogateescape'))

def normalize(s):
    s = s.replace('\\', '/').strip().strip('"\'')
    out = set()
    i = s.find('data/')
    if i >= 0:
        out.add(s[i:])
    else:
        out.add(s.lstrip('/'))
        out.add('data/' + s.lstrip('/'))
    return out

class Resolver:
    def __init__(self, names):
        self.names = set(names); self.found = {}
        self.suffixes = ['', '.import']
    def try_path(self, p):
        hit = False
        for suf in self.suffixes:
            q = p + suf; k = key(q)
            if k in self.names and k not in self.found:
                self.found[k] = q; hit = True
        return hit
    def try_string(self, s):
        hit = False
        for p in normalize(s):
            if self.try_path(p): hit = True
            if self.try_path(p.lower()): hit = True
        return hit
