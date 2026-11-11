M = (1 << 64) - 1
P = 0x100000001b3
def state(prefix, h=0xcbf29ce484222325):
    for c in prefix.lower().encode(): h = ((h ^ c) * P) & M
    return h
def cont(h, suffix):
    for c in suffix.encode(): h = ((h ^ c) * P) & M
    return h
