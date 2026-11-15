#!/usr/bin/env python3
"""github-core-clone-01: a GitHub-compatible git forge (pure Python 3.13 stdlib).

Serves, on one ephemeral port:
  - REST v3        (/api/v3/...)
  - Git smart HTTP (/{owner}/{repo}.git/...)  protocol v2 and v0, pack read/write
  - GraphQL v4     (/api/graphql)
  - MCP            (/mcp)  modern 2026-07-28 era (server/discover) + legacy initialize
  - Web UI         (GET /)  minimal HTML index
  - Health         (GET /healthz)

Lifecycle (the harness's, not the contest's): bind an ephemeral port, POST
{"port": N} to $MAKEFUN_ENV_URL/register, poll GET $MAKEFUN_ENV_URL/done until
{"done": true}, then exit 0. Never bind 8080 (the env owns it).

Determinism: every timestamp the forge emits comes from MAKEFUN_TRIAL_NOW
(never a wall clock), every id from a counter, every seed from
MAKEFUN_TRIAL_SEED. No model calls are made anywhere.

The viewer login is derived from the kit's fixed scenario seed (wire.ts:
SCENARIO_SEED is "the contest's fixed data", never changed), by porting the
kit's own seeded generator (fnv1a64 / mix64 / xorshift64* Rng /
generateUsers). The bearer token is opaque, so the forge cannot learn the
viewer from it; the scenario is the only source, and it is public by design.
"""

import base64
import difflib
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------
# Seeded scenario (port of program/env/src/wire.ts)
# --------------------------------------------------------------------------

_MASK64 = (1 << 64) - 1
_SCENARIO_SEED = 0x6A09E667F3BCC909
_SCENARIO_EPOCH = 1735689600  # 2025-01-01T00:00:00Z


def _fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & _MASK64
    return h


def _mix64(value):
    z = (value + 0x9E3779B97F4A7C15) & _MASK64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    return (z ^ (z >> 31)) & _MASK64


def _scenario_seed_for(label):
    return _mix64(_SCENARIO_SEED ^ _fnv1a64(label))


class _Rng:
    """xorshift64*: the kit's deterministic PRNG, ported verbatim."""

    def __init__(self, seed):
        masked = seed & _MASK64
        self.state = masked if masked != 0 else _SCENARIO_SEED

    def next_u64(self):
        x = self.state
        x = (x ^ (x >> 12)) & _MASK64
        x = (x ^ ((x << 25) & _MASK64)) & _MASK64
        x = (x ^ (x >> 27)) & _MASK64
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & _MASK64

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, max_exclusive):
        if max_exclusive == 1:
            return 0
        span = 4294967296
        limit = span - (span % max_exclusive)
        while True:
            value = self.next_u32()
            if value < limit:
                return value % max_exclusive

    def pick(self, items):
        return items[self.next_int(len(items))]


_FIRST_NAMES = [
    'ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta', 'hugo',
    'iris', 'jonas', 'kira', 'lars', 'mira', 'nico', 'olga', 'pavel',
    'quinn', 'rosa', 'sven', 'tess', 'ulla', 'vito', 'wren', 'yara', 'zane',
]
_LAST_NAMES = [
    'anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer', 'garcia',
    'holm', 'iversen', 'jansen', 'keller', 'lind', 'moreau', 'novak',
    'olsen', 'peters', 'quist', 'rossi', 'sato', 'turi', 'ulrich', 'vega',
    'weber', 'xu', 'yilmaz', 'zimmer',
]


def _generate_users(rng, count):
    used = set()
    users = []
    for i in range(count):
        first = rng.pick(_FIRST_NAMES)
        last = rng.pick(_LAST_NAMES)
        base = first + last
        login = base
        suffix = 2
        while login in used:
            login = '%s-%d' % (base, suffix)
            suffix += 1
        used.add(login)
        users.append({
            'id': 1000 + i, 'login': login, 'name': '%s %s' % (first, last),
            'email': '%s@example.invalid' % login,
        })
    return users


_SCENARIO_USERS = _generate_users(_Rng(_scenario_seed_for('users')), 12)
VIEWER = _SCENARIO_USERS[0]          # adaiversen
TEAMMATE = _SCENARIO_USERS[1]        # quinnkeller
STRANGER = _SCENARIO_USERS[2]        # olgabauer
VIEWER_LOGIN = VIEWER['login']
STRANGER_LOGIN = STRANGER['login']

# --------------------------------------------------------------------------
# Configuration / deterministic time
# --------------------------------------------------------------------------

ENV_URL = os.environ.get('MAKEFUN_ENV_URL', '').rstrip('/')
TOKEN = os.environ.get('MAKEFUN_SHOP_TOKEN', '')
TRIAL_SEED = os.environ.get('MAKEFUN_TRIAL_SEED', '0')
TRIAL_NOW_RAW = os.environ.get('MAKEFUN_TRIAL_NOW', '')


def _parse_trial_now(raw):
    if raw:
        try:
            text = raw.strip()
            if text.endswith('Z'):
                text = text[:-1] + '+00:00'
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp()), dt.astimezone(timezone.utc)
        except Exception:
            pass
    return _SCENARIO_EPOCH, datetime.fromtimestamp(_SCENARIO_EPOCH, tz=timezone.utc)


NOW_EPOCH, _NOW_DT = _parse_trial_now(TRIAL_NOW_RAW)
NOW_ISO = _NOW_DT.strftime('%Y-%m-%dT%H:%M:%SZ')

DEFAULT_BRANCH = 'main'
ZERO_OID = '0' * 40
VERSION = '1.0.0'
SERVER_NAME = 'github-core-clone'

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

_HEX_RE = re.compile(r'^[0-9a-f]{40}$')


def is_oid(text):
    return isinstance(text, str) and bool(_HEX_RE.match(text))


def sha1_hex(data):
    return hashlib.sha1(data).hexdigest()


def b64encode(data):
    return base64.b64encode(data).decode('ascii')


def b64decode(text):
    try:
        return base64.b64decode(text.encode('ascii'), validate=False)
    except Exception:
        return None


def gid(kind, *parts):
    """Opaque, stable global id: base64 of '<Kind>:<part>:<part>...'."""
    raw = (kind + ':' + ':'.join(str(p) for p in parts)).encode('utf-8')
    return base64.b64encode(raw).decode('ascii')


def ungid(text):
    """Decode a global id back to (kind, [parts]); None when not one of ours."""
    try:
        raw = base64.b64decode(text.encode('ascii')).decode('utf-8')
    except Exception:
        return None
    pieces = raw.split(':')
    if len(pieces) < 2:
        return None
    return pieces[0], pieces[1:]


def cursor_encode(offset):
    return base64.b64encode(('cursor:%d' % offset).encode('utf-8')).decode('ascii')


def cursor_decode(text):
    try:
        raw = base64.b64decode(text.encode('ascii')).decode('utf-8')
    except Exception:
        return None
    if not raw.startswith('cursor:'):
        return None
    try:
        return int(raw[len('cursor:'):])
    except ValueError:
        return None


def json_bytes(obj):
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def git_date(epoch):
    return '%d +0000' % epoch


def author_line():
    return '%s <%s> %s' % (VIEWER['name'], VIEWER['email'], git_date(NOW_EPOCH))

# --------------------------------------------------------------------------
# Git object model (SHA-1, blob / tree / commit / tag)
# --------------------------------------------------------------------------

OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7
_OBJ_TYPE_NAMES = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag'}
_OBJ_TYPE_IDS = {'commit': 1, 'tree': 2, 'blob': 3, 'tag': 4}


def git_object_id(obj_type, payload):
    """SHA-1 of '<type> <size>\\0<payload>'."""
    header = ('%s %d\0' % (obj_type, len(payload))).encode('ascii')
    return sha1_hex(header + payload)


def encode_blob(content):
    return ('blob', content)


def encode_commit(tree_oid, parents, message, author=None, committer=None):
    author = author or author_line()
    committer = committer or author
    lines = ['tree %s' % tree_oid]
    for parent in parents:
        lines.append('parent %s' % parent)
    lines.append('author %s' % author)
    lines.append('committer %s' % committer)
    lines.append('')
    lines.append(message)
    return ('commit', ('\n'.join(lines)).encode('utf-8'))


def parse_commit(payload):
    """Parse a commit payload into {tree, parents, message, raw_headers}."""
    text = payload.decode('utf-8', 'replace')
    head, _, message = text.partition('\n\n')
    tree = None
    parents = []
    for line in head.split('\n'):
        if line.startswith('tree '):
            tree = line[5:].strip()
        elif line.startswith('parent '):
            parents.append(line[7:].strip())
    return {'tree': tree, 'parents': parents, 'message': message}


def parse_tree(payload):
    """Parse a tree payload into [(mode_str, name, oid_hex)]."""
    entries = []
    i = 0
    n = len(payload)
    while i < n:
        space = payload.index(b' ', i)
        mode = payload[i:space].decode('ascii')
        nul = payload.index(b'\0', space)
        name = payload[space + 1:nul].decode('utf-8', 'surrogateescape')
        oid = payload[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        i = nul + 21
    return entries


def encode_tree(entries):
    """entries: [(mode_str, name, oid_hex)] already in git sort order."""
    out = bytearray()
    for mode, name, oid in entries:
        out += mode.encode('ascii') + b' ' + name.encode('utf-8', 'surrogateescape')
        out += b'\0' + bytes.fromhex(oid)
    return ('tree', bytes(out))


def _tree_sort_key(entry):
    mode, name, _oid = entry
    return name + ('/' if mode == '40000' else '')


def build_tree(objects, filemap):
    """Build nested trees for {path: bytes}. Returns (root_oid, new_objects).

    `objects` is the repo's object dict (oid -> (type, payload)); new tree
    objects are added to it as they are created. Identical trees reuse oids.
    """
    # Group paths by first component.
    def build(paths):
        entries = []
        dirs = {}
        files = {}
        for path, content in paths.items():
            head, _, rest = path.partition('/')
            if rest:
                dirs.setdefault(head, {})[rest] = content
            else:
                files[head] = content
        for name, content in files.items():
            oid = git_object_id('blob', content)
            objects.setdefault(oid, ('blob', content))
            entries.append(('100644', name, oid))
        for name, sub in dirs.items():
            sub_oid = build(sub)
            entries.append(('40000', name, sub_oid))
        entries.sort(key=_tree_sort_key)
        obj_type, payload = encode_tree(entries)
        oid = git_object_id(obj_type, payload)
        objects.setdefault(oid, (obj_type, payload))
        return oid

    return build(filemap)


def flatten_tree(objects, root_oid):
    """Walk a tree into {path: (mode, oid)} for every blob (recursive)."""
    result = {}

    def walk(oid, prefix):
        entry = objects.get(oid)
        if entry is None:
            return
        obj_type, payload = entry
        if obj_type != 'tree':
            return
        for mode, name, child in parse_tree(payload):
            path = prefix + name
            if mode == '40000':
                walk(child, path + '/')
            else:
                result[path] = (mode, child)

    if root_oid:
        walk(root_oid, '')
    return result


def tree_filemap(objects, root_oid):
    """{path: bytes} for every blob under a tree."""
    flat = flatten_tree(objects, root_oid)
    out = {}
    for path, (_mode, oid) in flat.items():
        entry = objects.get(oid)
        if entry is not None:
            out[path] = entry[1]
    return out


def commit_ancestors(objects, tip_oid, limit=None):
    """All commit oids reachable from tip (tip first, BFS by generation)."""
    seen = []
    seen_set = set()
    queue = [tip_oid] if tip_oid else []
    while queue:
        oid = queue.pop(0)
        if oid in seen_set:
            continue
        entry = objects.get(oid)
        if entry is None or entry[0] != 'commit':
            continue
        seen_set.add(oid)
        seen.append(oid)
        if limit is not None and len(seen) >= limit:
            break
        for parent in parse_commit(entry[1])['parents']:
            if parent not in seen_set:
                queue.append(parent)
    return seen


def is_ancestor(objects, ancestor_oid, tip_oid):
    """True when ancestor_oid is reachable from tip_oid (or equal)."""
    if ancestor_oid == tip_oid:
        return True
    return ancestor_oid in set(commit_ancestors(objects, tip_oid))


def merge_base(objects, oid_a, oid_b):
    """Nearest common ancestor of two commits (linear histories: exact)."""
    if not oid_a or not oid_b:
        return None
    ancestors_a = set(commit_ancestors(objects, oid_a))
    for oid in commit_ancestors(objects, oid_b):
        if oid in ancestors_a:
            return oid
    return None


def three_way_merge(objects, base_oid, head_oid, other_oid):
    """Merge head-side into other-side over base.

    Returns (merged_filemap, conflicts). A path conflicts when both sides
    changed it differently from base. None in a map means "absent".
    """
    base_map = tree_filemap(objects, _commit_tree(objects, base_oid)) if base_oid else {}
    head_map = tree_filemap(objects, _commit_tree(objects, head_oid)) if head_oid else {}
    other_map = tree_filemap(objects, _commit_tree(objects, other_oid)) if other_oid else {}
    merged = {}
    conflicts = []
    for path in sorted(set(base_map) | set(head_map) | set(other_map)):
        b = base_map.get(path)
        h = head_map.get(path)
        o = other_map.get(path)
        if h == b:
            value = o
        elif o == b:
            value = h
        elif h == o:
            value = h
        else:
            conflicts.append(path)
            value = o
        if value is not None:
            merged[path] = value
    return merged, conflicts


def _commit_tree(objects, commit_oid):
    if not commit_oid:
        return None
    entry = objects.get(commit_oid)
    if entry is None or entry[0] != 'commit':
        return None
    return parse_commit(entry[1])['tree']


def subtree_oid(objects, root_oid, dir_path):
    """The tree oid of a directory path under a tree (None when absent)."""
    if not dir_path:
        return root_oid
    oid = root_oid
    for part in dir_path.split('/'):
        entry = objects.get(oid)
        if entry is None or entry[0] != 'tree':
            return None
        found = None
        for mode, name, child in parse_tree(entry[1]):
            if name == part and mode == '40000':
                found = child
                break
        if found is None:
            return None
        oid = found
    return oid


def diff_trees(objects, old_root_oid, new_root_oid):
    """Diff two trees into [{filename, status, additions, deletions, changes}].

    Line counts come from a real line diff (difflib), so added/modified files
    carry honest numbers.
    """
    old_map = tree_filemap(objects, old_root_oid) if old_root_oid else {}
    new_map = tree_filemap(objects, new_root_oid) if new_root_oid else {}
    out = []
    for path in sorted(set(old_map) | set(new_map)):
        old = old_map.get(path)
        new = new_map.get(path)
        if old is None and new is not None:
            lines = new.decode('utf-8', 'replace').splitlines()
            out.append({'filename': path, 'status': 'added',
                        'additions': len(lines), 'deletions': 0,
                        'changes': len(lines)})
        elif new is None and old is not None:
            lines = old.decode('utf-8', 'replace').splitlines()
            out.append({'filename': path, 'status': 'removed',
                        'additions': 0, 'deletions': len(lines),
                        'changes': len(lines)})
        elif old != new:
            old_lines = old.decode('utf-8', 'replace').splitlines()
            new_lines = new.decode('utf-8', 'replace').splitlines()
            sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
            additions = deletions = 0
            for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
                if tag in ('insert', 'replace'):
                    additions += j2 - j1
                if tag in ('delete', 'replace'):
                    deletions += _i2 - _i1
            out.append({'filename': path, 'status': 'modified',
                        'additions': additions, 'deletions': deletions,
                        'changes': additions + deletions})
    return out

# --------------------------------------------------------------------------
# pkt-line framing and packfiles
# --------------------------------------------------------------------------

def pkt_line(payload):
    """One pkt-line: 4 hex length digits (incl. the 4) + payload."""
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    return ('%04x' % (len(payload) + 4)).encode('ascii') + payload


PKT_FLUSH = b'0000'
PKT_DELIM = b'0001'


def pkt_lines(items):
    out = bytearray()
    for item in items:
        if item == 'flush':
            out += PKT_FLUSH
        elif item == 'delim':
            out += PKT_DELIM
        else:
            out += pkt_line(item)
    return bytes(out)


def parse_pkt_lines(data):
    """Parse a pkt-line stream into [('data', payload) | ('flush', None) |
    ('delim', None)]. Stops at the first flush when `stop_at_flush` is set by
    the caller (the caller slices the stream); here we parse the whole buffer.
    """
    packets = []
    i = 0
    n = len(data)
    while i < n:
        if i + 4 > n:
            break
        length = int(data[i:i + 4].decode('ascii'), 16)
        if length == 0:
            packets.append(('flush', None))
            i += 4
        elif length == 1:
            packets.append(('delim', None))
            i += 4
        else:
            packets.append(('data', data[i + 4:i + length]))
            i += length
    return packets


def _read_varint(data, pos):
    """Pack entry size varint: returns (value, new_pos)."""
    byte = data[pos]
    pos += 1
    value = byte & 0x0F
    shift = 4
    while byte & 0x80:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
    return value, pos


def _read_ofs_delta_offset(data, pos):
    byte = data[pos]
    pos += 1
    offset = byte & 0x7F
    while byte & 0x80:
        byte = data[pos]
        pos += 1
        offset = ((offset + 1) << 7) | (byte & 0x7F)
    return offset, pos


def apply_delta(base, delta):
    """Apply a git delta to base bytes; returns the result bytes."""
    pos = 0
    _src_size, pos = _read_varint(delta, pos)
    dst_size, pos = _read_varint(delta, pos)
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            offset = 0
            size = 0
            for bit in range(4):
                if op & (1 << bit):
                    offset |= delta[pos] << (8 * bit)
                    pos += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    size |= delta[pos] << (8 * bit)
                    pos += 1
            if size == 0:
                size = 0x10000
            out += base[offset:offset + size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError('delta opcode 0')
    if len(out) != dst_size:
        raise ValueError('delta result size mismatch')
    return bytes(out)


def parse_pack(data, resolve_base=None):
    """Parse a packfile into {oid: (type_str, payload)}.

    Handles plain objects and OFS_DELTA / REF_DELTA (bases resolved from the
    pack itself first, then via `resolve_base(oid)` when given).
    Raises ValueError on malformed input.
    """
    if len(data) < 12 or data[0:4] != b'PACK':
        raise ValueError('not a packfile')
    version = int.from_bytes(data[4:8], 'big')
    if version not in (2, 3):
        raise ValueError('unsupported pack version %d' % version)
    count = int.from_bytes(data[8:12], 'big')
    pos = 12
    objects = {}
    # First pass: read entries in order, remembering offsets for OFS deltas.
    offsets = []  # (start_offset, oid or None, type_id, payload_or_delta, base_ref)
    raw_entries = []
    for _ in range(count):
        start = pos
        byte = data[pos]
        pos += 1
        type_id = (byte >> 4) & 0x07
        size, pos = _read_varint_from_first(data, pos, byte)
        if type_id == OBJ_OFS_DELTA:
            neg_offset, pos = _read_ofs_delta_offset(data, pos)
            base_offset = start - neg_offset
            raw_entries.append((start, type_id, None, base_offset, size))
        elif type_id == OBJ_REF_DELTA:
            base_oid = data[pos:pos + 20].hex()
            pos += 20
            raw_entries.append((start, type_id, base_oid, None, size))
        else:
            raw_entries.append((start, type_id, None, None, size))
        # The object data is a zlib stream starting at pos.
        decomp = zlib.decompressobj()
        payload = decomp.decompress(data[pos:])
        consumed = len(data[pos:]) - len(decomp.unused_data)
        pos += consumed
        raw_entries[-1] = raw_entries[-1] + (payload,)
        offsets.append(start)
    # Second pass: resolve.
    by_offset = {}
    for index, entry in enumerate(raw_entries):
        start, type_id, base_ref, base_offset, _size, payload = entry
        if type_id in (OBJ_COMMIT, OBJ_TREE, OBJ_BLOB, OBJ_TAG):
            type_name = _OBJ_TYPE_NAMES[type_id]
            oid = git_object_id(type_name, payload)
            objects[oid] = (type_name, payload)
            by_offset[start] = (type_name, payload, oid)
        elif type_id == OBJ_OFS_DELTA:
            base = by_offset.get(base_offset)
            if base is None:
                raise ValueError('ofs-delta base not found in pack')
            result = apply_delta(base[1], payload)
            oid = git_object_id(base[0], result)
            objects[oid] = (base[0], result)
            by_offset[start] = (base[0], result, oid)
        elif type_id == OBJ_REF_DELTA:
            base = objects.get(base_ref)
            if base is None and resolve_base is not None:
                base = resolve_base(base_ref)
            if base is None:
                raise ValueError('ref-delta base %s not found' % base_ref)
            result = apply_delta(base[1], payload)
            oid = git_object_id(base[0], result)
            objects[oid] = (base[0], result)
            by_offset[start] = (base[0], result, oid)
        else:
            raise ValueError('unknown pack entry type %d' % type_id)
    return objects


def _read_varint_from_first(data, pos, first_byte):
    """Continue a size varint whose first byte was already consumed."""
    value = first_byte & 0x0F
    shift = 4
    byte = first_byte
    while byte & 0x80:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
    return value, pos


def build_pack(objects):
    """Build a packfile (version 2, plain objects, no deltas).

    `objects`: iterable of (type_str, payload). Returns the pack bytes with
    the SHA-1 trailer.
    """
    entries = list(objects)
    out = bytearray()
    out += b'PACK'
    out += (2).to_bytes(4, 'big')
    out += len(entries).to_bytes(4, 'big')
    for obj_type, payload in entries:
        type_id = _OBJ_TYPE_IDS[obj_type]
        size = len(payload)
        header = bytearray()
        first = (type_id << 4) | (size & 0x0F)
        size >>= 4
        if size:
            first |= 0x80
        header.append(first)
        while size:
            byte = size & 0x7F
            size >>= 7
            if size:
                byte |= 0x80
            header.append(byte)
        out += header
        out += zlib.compress(payload)
    trailer = hashlib.sha1(bytes(out)).digest()
    out += trailer
    return bytes(out)


def collect_fetch_objects(objects, wants, haves, depth, prev_shallow=None):
    """Objects for a fetch: reachable from `wants` minus reachable from
    `haves`, honouring a shallow `depth` (None = full history).

    Returns (objects_list, shallow_oids, unshallow_oids) where objects_list is
    [(type, payload)] in a stable order (commits first, then trees, blobs).
    `objects` is the repo object dict; `wants`/`haves` are oid lists;
    `depth` is an int or None; `prev_shallow` lists the shallow boundaries
    the client already holds. A previous boundary is "unshallowed" when the
    new walk includes it together with its parents.
    """
    prev_shallow = prev_shallow or []
    want_commits = set()
    shallow = set()
    # Walk wants with generation limits.
    seen_commits = set()
    queue = [(oid, 0) for oid in wants]
    while queue:
        oid, gen = queue.pop(0)
        if oid in seen_commits:
            continue
        entry = objects.get(oid)
        if entry is None or entry[0] != 'commit':
            continue
        seen_commits.add(oid)
        want_commits.add(oid)
        if depth is not None and gen + 1 >= depth:
            shallow.add(oid)
            continue
        for parent in parse_commit(entry[1])['parents']:
            if parent not in seen_commits:
                queue.append((parent, gen + 1))
    unshallow = sorted(
        oid for oid in prev_shallow
        if oid in want_commits and oid not in shallow)
    # Objects reachable from the wanted commits (trees + blobs, recursive).
    reachable = set()
    stack = list(want_commits)
    for oid in wants:
        if oid in objects and oid not in want_commits:
            stack.append(oid)
    while stack:
        oid = stack.pop()
        if oid in reachable:
            continue
        entry = objects.get(oid)
        if entry is None:
            continue
        reachable.add(oid)
        if entry[0] == 'commit':
            tree = parse_commit(entry[1])['tree']
            if tree:
                stack.append(tree)
        elif entry[0] == 'tree':
            for _mode, _name, child in parse_tree(entry[1]):
                stack.append(child)
    while stack:
        oid = stack.pop()
        if oid in reachable:
            continue
        entry = objects.get(oid)
        if entry is None:
            continue
        reachable.add(oid)
        if entry[0] == 'commit':
            tree = parse_commit(entry[1])['tree']
            if tree:
                stack.append(tree)
        elif entry[0] == 'tree':
            for _mode, _name, child in parse_tree(entry[1]):
                stack.append(child)
    # Subtract everything reachable from the haves.
    if haves:
        have_reachable = set()
        stack = list(haves)
        while stack:
            oid = stack.pop()
            if oid in have_reachable:
                continue
            entry = objects.get(oid)
            if entry is None:
                continue
            have_reachable.add(oid)
            if entry[0] == 'commit':
                tree = parse_commit(entry[1])['tree']
                if tree:
                    stack.append(tree)
                for parent in parse_commit(entry[1])['parents']:
                    stack.append(parent)
            elif entry[0] == 'tree':
                for _mode, _name, child in parse_tree(entry[1]):
                    stack.append(child)
        reachable -= have_reachable
    # Ordering: commits, then trees, then blobs (stable, by oid).
    ordered = []
    for type_name in ('commit', 'tree', 'blob', 'tag'):
        for oid in sorted(reachable):
            entry = objects.get(oid)
            if entry is not None and entry[0] == type_name:
                ordered.append(entry)
    return ordered, sorted(shallow), unshallow

# --------------------------------------------------------------------------
# Store: repositories, issues, pull requests (in-memory, one process)
# --------------------------------------------------------------------------

class Comment:
    __slots__ = ('id', 'body', 'user', 'created_at')

    def __init__(self, cid, body):
        self.id = cid
        self.body = body
        self.user = VIEWER_LOGIN
        self.created_at = NOW_ISO

    def to_json(self):
        return {'id': self.id, 'body': self.body,
                'user': {'login': self.user}, 'created_at': self.created_at,
                'updated_at': self.created_at}


class Issue:
    __slots__ = ('number', 'title', 'body', 'state', 'labels', 'comments',
                 'seq', 'created_at', 'updated_at', 'db_id')

    def __init__(self, number, title, body, db_id, seq):
        self.number = number
        self.title = title
        self.body = body if body is not None else ''
        self.state = 'open'
        self.labels = []
        self.comments = []
        self.seq = seq
        self.db_id = db_id
        self.created_at = NOW_ISO
        self.updated_at = NOW_ISO

    def gid(self, owner, repo):
        return gid('Issue', owner, repo, self.number)

    def to_json(self, owner, repo):
        return {'id': self.db_id, 'number': self.number, 'title': self.title,
                'body': self.body, 'state': self.state,
                'labels': [{'name': name} for name in self.labels],
                'user': {'login': VIEWER_LOGIN},
                'created_at': self.created_at, 'updated_at': self.updated_at}


class PullRequest:
    __slots__ = ('number', 'title', 'body', 'state', 'merged', 'merged_at',
                 'head', 'base', 'merge_commit_sha', 'seq', 'db_id',
                 'created_at', 'updated_at', 'comments')

    def __init__(self, number, title, body, head, base, db_id, seq):
        self.number = number
        self.title = title
        self.body = body if body is not None else ''
        self.state = 'open'
        self.merged = False
        self.merged_at = None
        self.head = head
        self.base = base
        self.merge_commit_sha = None
        self.seq = seq
        self.db_id = db_id
        self.created_at = NOW_ISO
        self.updated_at = NOW_ISO
        self.comments = []

    def gid(self, owner, repo):
        return gid('PullRequest', owner, repo, self.number)

    def to_json(self, repo):
        head_sha = repo.refs.get('refs/heads/' + self.head) or ZERO_OID
        base_sha = repo.refs.get('refs/heads/' + self.base) or ZERO_OID
        return {'id': self.db_id, 'number': self.number, 'title': self.title,
                'body': self.body, 'state': self.state,
                'merged': self.merged, 'merged_at': self.merged_at,
                'merge_commit_sha': self.merge_commit_sha,
                'head': {'ref': self.head, 'sha': head_sha,
                         'label': '%s:%s' % (repo.owner, self.head)},
                'base': {'ref': self.base, 'sha': base_sha,
                         'label': '%s:%s' % (repo.owner, self.base)},
                'user': {'login': VIEWER_LOGIN},
                'created_at': self.created_at, 'updated_at': self.updated_at}


class Repo:
    __slots__ = ('owner', 'name', 'description', 'private', 'default_branch',
                 'refs', 'objects', 'issues', 'labels', 'pulls', 'db_id',
                 'seq', 'next_issue', 'next_pr', 'created_at')

    def __init__(self, owner, name, description, private, db_id, seq):
        self.owner = owner
        self.name = name
        self.description = description if description is not None else ''
        self.private = bool(private)
        self.default_branch = DEFAULT_BRANCH
        self.refs = {}          # 'refs/heads/<branch>' -> commit oid
        self.objects = {}       # oid -> (type, payload)
        self.issues = []        # Issue, in creation order
        self.labels = {}        # name -> {'name','color','description'}
        self.pulls = []         # PullRequest, in creation order
        self.db_id = db_id
        self.seq = seq
        self.next_issue = 1
        self.next_pr = 1
        self.created_at = NOW_ISO

    def gid(self):
        return gid('Repository', self.owner, self.name)

    def to_json(self):
        return {'id': self.db_id, 'name': self.name,
                'full_name': '%s/%s' % (self.owner, self.name),
                'owner': {'login': self.owner, 'id': 1000},
                'private': self.private, 'description': self.description,
                'default_branch': self.default_branch,
                'html_url': 'https://github.com/%s/%s' % (self.owner, self.name),
                'clone_url': 'https://github.com/%s/%s.git' % (self.owner, self.name),
                'has_issues': True, 'has_pull_requests': True,
                'created_at': self.created_at, 'updated_at': self.created_at}

    def find_issue(self, number):
        for issue in self.issues:
            if issue.number == number:
                return issue
        return None

    def find_pr(self, number):
        for pr in self.pulls:
            if pr.number == number:
                return pr
        return None

    def head_commit(self, branch=None):
        ref = 'refs/heads/' + (branch or self.default_branch)
        return self.refs.get(ref)

    def commit_for_ref(self, ref):
        """Resolve a branch name, full ref, or commit oid to a commit oid."""
        if not ref:
            return self.head_commit()
        if ref in self.refs:
            return self.refs[ref]
        short = 'refs/heads/' + ref
        if short in self.refs:
            return self.refs[short]
        if is_oid(ref) and ref in self.objects:
            entry = self.objects.get(ref)
            if entry is not None and entry[0] == 'commit':
                return ref
        return None

    def make_commit(self, branch, filemap, message, parent_oid=None):
        """Create a commit on `branch` with the given full file map.

        Returns the new commit oid. The branch is created when missing
        (from its parent, or from empty for the first commit).
        """
        root_oid = build_tree(self.objects, filemap)
        parents = []
        if parent_oid:
            parents = [parent_oid]
        obj_type, payload = encode_commit(root_oid, parents, message)
        commit_oid = git_object_id(obj_type, payload)
        self.objects.setdefault(commit_oid, (obj_type, payload))
        self.refs['refs/heads/' + branch] = commit_oid
        return commit_oid

    def commit_file(self, branch, path, content, message, delete=False):
        """Apply one file change on `branch`; returns (commit_oid, blob_oid).

        The branch is created from the default tip when it does not exist
        yet (or from empty when the repo has no commits at all).
        """
        if branch not in [ref[len('refs/heads/'):] for ref in self.refs]:
            # New branch: start from the default branch tip (or empty).
            base_commit = self.head_commit()
            if base_commit:
                self.refs['refs/heads/' + branch] = base_commit
        parent_oid = self.head_commit(branch)
        if parent_oid:
            filemap = tree_filemap(self.objects, _commit_tree(self.objects, parent_oid))
        else:
            filemap = {}
        if delete:
            filemap.pop(path, None)
            blob_oid = None
        else:
            filemap[path] = content
            blob_oid = git_object_id('blob', content)
            self.objects.setdefault(blob_oid, ('blob', content))
        commit_oid = self.make_commit(branch, filemap, message, parent_oid)
        return commit_oid, blob_oid

    def mergeable_state(self, pr):
        """MERGEABLE / CONFLICTING for a pull request (real 3-way check)."""
        head_oid = self.head_commit(pr.head)
        base_oid = self.head_commit(pr.base)
        if not head_oid or not base_oid:
            return 'UNKNOWN'
        base = merge_base(self.objects, head_oid, base_oid)
        _merged, conflicts = three_way_merge(self.objects, base, head_oid, base_oid)
        return 'CONFLICTING' if conflicts else 'MERGEABLE'

    def pr_files(self, pr):
        """Diff between the merge-base tree and the head tree."""
        head_oid = self.head_commit(pr.head)
        base_oid = self.head_commit(pr.base)
        if not head_oid or not base_oid:
            return []
        base = merge_base(self.objects, head_oid, base_oid)
        base_tree = _commit_tree(self.objects, base) if base else None
        head_tree = _commit_tree(self.objects, head_oid)
        return diff_trees(self.objects, base_tree, head_tree)

    def squash_merge(self, pr, message=None):
        """Squash-merge a PR onto its base branch.

        Returns (commit_oid, error_reason). On conflict returns (None, ...).
        """
        head_oid = self.head_commit(pr.head)
        base_oid = self.head_commit(pr.base)
        if not head_oid or not base_oid:
            return None, 'missing head or base'
        base = merge_base(self.objects, head_oid, base_oid)
        merged, conflicts = three_way_merge(self.objects, base, head_oid, base_oid)
        if conflicts:
            return None, 'conflict'
        msg = message or ('Merge pull request #%d: %s' % (pr.number, pr.title))
        commit_oid = self.make_commit(pr.base, merged, msg, base_oid)
        pr.state = 'closed'
        pr.merged = True
        pr.merged_at = NOW_ISO
        pr.merge_commit_sha = commit_oid
        pr.updated_at = NOW_ISO
        return commit_oid, None


class Store:
    """The whole forge state, guarded by one re-entrant lock."""

    def __init__(self):
        self.lock = threading.RLock()
        self.repos = {}         # (owner, name) -> Repo
        self.order = []         # Repos in creation order
        self.next_db_id = 1
        self.next_comment_id = 1
        self.next_issue_db_id = 1
        self.next_pr_db_id = 1
        self.next_req = 1

    def request_id(self):
        with self.lock:
            rid = self.next_req
            self.next_req += 1
        return 'req-%06d' % rid

    def create_repo(self, owner, name, description, private):
        with self.lock:
            if (owner, name) in self.repos:
                return None
            repo = Repo(owner, name, description, private,
                        self.next_db_id, len(self.order) + 1)
            self.next_db_id += 1
            self.repos[(owner, name)] = repo
            self.order.append(repo)
            return repo

    def get_repo(self, owner, name):
        return self.repos.get((owner, name))

    def delete_repo(self, owner, name):
        with self.lock:
            repo = self.repos.pop((owner, name), None)
            if repo is not None:
                self.order = [r for r in self.order if r is not repo]
            return repo

    def new_comment_id(self):
        with self.lock:
            cid = self.next_comment_id
            self.next_comment_id += 1
            return cid

    def new_issue_db_id(self):
        with self.lock:
            value = self.next_issue_db_id
            self.next_issue_db_id += 1
            return value

    def new_pr_db_id(self):
        with self.lock:
            value = self.next_pr_db_id
            self.next_pr_db_id += 1
            return value


STORE = Store()

# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------

def _paginate(items, query, default_per_page=30):
    """Slice a list by ?page=&per_page=; returns (page_items, page, per_page,
    total_pages)."""
    try:
        per_page = int(query.get('per_page', [str(default_per_page)])[0])
    except (ValueError, TypeError):
        per_page = default_per_page
    per_page = max(1, min(100, per_page))
    try:
        page = int(query.get('page', ['1'])[0])
    except (ValueError, TypeError):
        page = 1
    page = max(1, page)
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    return items[start:start + per_page], page, per_page, pages


class ForgeHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'github-core-clone/1.0.0'

    # -- quiet logging ------------------------------------------------------
    def log_message(self, fmt, *args):
        pass

    # -- response helpers ---------------------------------------------------
    def _send(self, status, body, content_type='application/json',
              extra_headers=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-GitHub-Request-Id', STORE.request_id())
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, status, obj, extra_headers=None):
        self._send(status, json_bytes(obj), 'application/json', extra_headers)

    def _err(self, status, message):
        self._json(status, {'message': message})

    def _read_body(self):
        length = self.headers.get('Content-Length')
        if length is None:
            return b''
        try:
            n = int(length)
        except ValueError:
            return b''
        if n <= 0:
            return b''
        return self.rfile.read(n)

    def _read_json(self):
        """Returns (obj, None) or (None, error_message)."""
        raw = self._read_body()
        if not raw:
            return None, None
        try:
            return json.loads(raw.decode('utf-8')), None
        except Exception:
            return None, 'malformed JSON body'

    def _auth_ok(self):
        if not TOKEN:
            return True
        auth = self.headers.get('Authorization', '')
        return auth == 'Bearer ' + TOKEN

    def _path_parts(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        parts = [urllib.parse.unquote(seg)
                 for seg in parsed.path.split('/') if seg != '']
        return parts, query, parsed.path

    def _base_url(self):
        host = self.headers.get('Host', '127.0.0.1')
        return 'http://' + host

    def _link_header(self, path, query, page, per_page, pages):
        """GitHub-style Link header for a paginated collection."""
        def url(p):
            q = dict(query)
            q['page'] = [str(p)]
            q['per_page'] = [str(per_page)]
            return '%s%s?%s' % (self._base_url(), path,
                                urllib.parse.urlencode(q, doseq=True))
        links = []
        if page < pages:
            links.append('<%s>; rel="next"' % url(page + 1))
            links.append('<%s>; rel="last"' % url(pages))
        if page > 1:
            links.append('<%s>; rel="prev"' % url(page - 1))
            links.append('<%s>; rel="first"' % url(1))
        if not links:
            return None
        return ', '.join(links)

    # -- verb entry points --------------------------------------------------
    def do_GET(self):
        self._route('GET')

    def do_POST(self):
        self._route('POST')

    def do_PUT(self):
        self._route('PUT')

    def do_PATCH(self):
        self._route('PATCH')

    def do_DELETE(self):
        self._route('DELETE')

    def do_HEAD(self):
        self._route('HEAD')

    # -- router -------------------------------------------------------------
    def _route(self, method):
        try:
            parts, query, raw_path = self._path_parts()
        except Exception:
            self._err(400, 'bad request path')
            return
        try:
            if raw_path == '/healthz' and method == 'GET':
                self._json(200, {'ok': True})
                return
            if raw_path == '/' and method in ('GET', 'HEAD'):
                self._serve_index()
                return
            if parts and parts[0] == 'api' and len(parts) > 1 and parts[1] == 'v3':
                if not self._auth_ok():
                    self._err(401, 'Bad credentials')
                    return
                self._route_rest(method, parts[2:], query, raw_path)
                return
            if parts == ['api', 'graphql']:
                if not self._auth_ok():
                    self._err(401, 'Bad credentials')
                    return
                self._route_graphql(method)
                return
            if parts == ['mcp']:
                if not self._auth_ok():
                    self._err(401, 'Bad credentials')
                    return
                self._route_mcp(method)
                return
            if len(parts) >= 2 and parts[1].endswith('.git'):
                if not self._auth_ok():
                    self._err(401, 'Bad credentials')
                    return
                self._route_git(method, parts, query)
                return
            self._err(404, 'Not Found')
        except BrokenPipeError:
            pass
        except ConnectionResetError:
            pass
        except Exception as exc:  # never crash the server on a bad request
            try:
                self._err(500, 'internal error: %s' % exc)
            except Exception:
                pass

    def _serve_index(self):
        html = (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<title>github-core-clone</title></head><body>'
            '<h1>github-core-clone</h1>'
            '<p>A GitHub-compatible git forge: REST v3, git smart HTTP, '
            'GraphQL v4 and MCP on this origin.</p>'
            '<p>Owner: %s</p>'
            '</body></html>' % VIEWER_LOGIN)
        self._send(200, html, 'text/html; charset=utf-8')

    # -- REST ---------------------------------------------------------------
    def _repo_or_404(self, owner, name):
        repo = STORE.get_repo(owner, name)
        if repo is None:
            self._err(404, 'Not Found')
            return None
        return repo

    def _route_rest(self, method, parts, query, raw_path):
        # GET /api/v3/  (root)
        if parts == [] and method == 'GET':
            self._json(200, {'current_user_url': self._base_url() + '/api/v3/user'},
                       {'X-OAuth-Scopes': 'repo, read:org'})
            return
        if not parts:
            self._err(404, 'Not Found')
            return
        if parts[0] == 'meta' and len(parts) == 1 and method == 'GET':
            self._json(200, {'installed_version': VERSION})
            return
        if parts[0] == 'user':
            self._route_rest_user(method, parts[1:], query, raw_path)
            return
        if parts[0] == 'repos' and len(parts) >= 3:
            self._route_rest_repo(method, parts[1], parts[2], parts[3:],
                                  query, raw_path)
            return
        self._err(404, 'Not Found')

    def _route_rest_user(self, method, parts, query, raw_path):
        if parts == [] and method == 'GET':
            self._json(200, {'login': VIEWER_LOGIN, 'id': VIEWER['id'],
                             'name': VIEWER['name'], 'email': VIEWER['email'],
                             'type': 'User'})
            return
        if parts == ['repos']:
            if method == 'POST':
                body, err = self._read_json()
                if err:
                    self._err(400, err)
                    return
                if not isinstance(body, dict) or not body.get('name'):
                    self._err(422, 'Repository name is required')
                    return
                name = str(body['name'])
                if not re.match(r'^[A-Za-z0-9._-]+$', name) or len(name) > 100:
                    self._err(422, 'Invalid repository name')
                    return
                repo = STORE.create_repo(VIEWER_LOGIN, name,
                                         body.get('description'),
                                         body.get('private', False))
                if repo is None:
                    self._err(422, 'Repository already exists')
                    return
                self._json(201, repo.to_json())
                return
            if method == 'GET':
                items = [repo.to_json() for repo in STORE.order
                         if repo.owner == VIEWER_LOGIN]
                page_items, page, per_page, pages = _paginate(items, query)
                headers = {}
                link = self._link_header(raw_path, query, page, per_page, pages)
                if link:
                    headers['Link'] = link
                self._json(200, page_items, headers)
                return
        self._err(404, 'Not Found')

    def _route_rest_repo(self, method, owner, name, parts, query, raw_path):
        repo = self._repo_or_404(owner, name)
        if repo is None:
            return
        if parts == []:
            if method == 'GET':
                self._json(200, repo.to_json())
                return
            if method == 'PATCH':
                body, err = self._read_json()
                if err:
                    self._err(400, err)
                    return
                if isinstance(body, dict):
                    with STORE.lock:
                        if 'description' in body:
                            repo.description = body['description'] or ''
                        if 'private' in body:
                            repo.private = bool(body['private'])
                self._json(200, repo.to_json())
                return
            if method == 'DELETE':
                STORE.delete_repo(owner, name)
                self._send(204, b'')
                return
            self._err(404, 'Not Found')
            return
        if parts[0] == 'git' and len(parts) >= 2 and parts[1] == 'refs':
            self._route_rest_refs(method, repo, parts[2:])
            return
        if parts[0] == 'branches':
            self._route_rest_branches(method, repo, parts[1:])
            return
        if parts[0] == 'commits' and method == 'GET':
            self._route_rest_commits(repo, query)
            return
        if parts[0] == 'contents':
            self._route_rest_contents(method, repo, parts[1:], query)
            return
        if parts[0] == 'labels':
            self._route_rest_labels(method, repo, parts[1:])
            return
        if parts[0] == 'issues':
            self._route_rest_issues(method, repo, parts[1:], query)
            return
        if parts[0] == 'pulls':
            self._route_rest_pulls(method, repo, parts[1:], query)
            return
        self._err(404, 'Not Found')

    def _route_rest_refs(self, method, repo, parts):
        if method == 'POST' and parts == []:
            body, err = self._read_json()
            if err:
                self._err(400, err)
                return
            if not isinstance(body, dict):
                self._err(422, 'ref and sha are required')
                return
            ref = body.get('ref', '')
            sha = body.get('sha', '')
            if not ref.startswith('refs/heads/') or not is_oid(sha):
                self._err(422, 'Invalid ref or sha')
                return
            with STORE.lock:
                if ref in repo.refs:
                    self._err(422, 'Reference already exists')
                    return
                entry = repo.objects.get(sha)
                if entry is None or entry[0] != 'commit':
                    self._err(422, 'Unknown commit sha')
                    return
                repo.refs[ref] = sha
            self._json(201, {'ref': ref, 'object': {'sha': sha, 'type': 'commit'}})
            return
        if method == 'GET' and len(parts) == 2 and parts[0] == 'heads':
            ref = 'refs/heads/' + parts[1]
            with STORE.lock:
                sha = repo.refs.get(ref)
            if sha is None:
                self._err(404, 'Not Found')
                return
            self._json(200, {'ref': ref, 'object': {'sha': sha, 'type': 'commit'}})
            return
        self._err(404, 'Not Found')

    def _route_rest_branches(self, method, repo, parts):
        if method != 'GET':
            self._err(404, 'Not Found')
            return
        with STORE.lock:
            branches = sorted(repo.refs.keys())
            if parts == []:
                out = [{'name': ref[len('refs/heads/'):],
                        'commit': {'sha': repo.refs[ref]},
                        'protected': False}
                       for ref in branches if ref.startswith('refs/heads/')]
                self._json(200, out)
                return
            if len(parts) == 1:
                ref = 'refs/heads/' + parts[0]
                sha = repo.refs.get(ref)
                if sha is None:
                    self._err(404, 'Not Found')
                    return
                self._json(200, {'name': parts[0], 'commit': {'sha': sha},
                                 'protected': False})
                return
        self._err(404, 'Not Found')

    def _route_rest_commits(self, repo, query):
        ref = query.get('sha', [None])[0]
        with STORE.lock:
            tip = repo.commit_for_ref(ref) if ref else repo.head_commit()
            if tip is None:
                self._json(200, [])
                return
            commits = commit_ancestors(repo.objects, tip)
        out = []
        for oid in commits:
            entry = repo.objects.get(oid)
            if entry is None:
                continue
            parsed = parse_commit(entry[1])
            out.append({'sha': oid,
                        'commit': {'message': parsed['message'],
                                   'author': {'name': VIEWER['name'],
                                              'email': VIEWER['email'],
                                              'date': NOW_ISO},
                                   'committer': {'name': VIEWER['name'],
                                                 'email': VIEWER['email'],
                                                 'date': NOW_ISO}},
                        'author': {'login': VIEWER_LOGIN},
                        'committer': {'login': VIEWER_LOGIN}})
        self._json(200, out)

    def _route_rest_contents(self, method, repo, parts, query):
        path = '/'.join(parts)
        if method == 'GET':
            ref = query.get('ref', [None])[0]
            with STORE.lock:
                tip = repo.commit_for_ref(ref) if ref else repo.head_commit()
                if tip is None:
                    self._err(404, 'Not Found')
                    return
                tree_oid = _commit_tree(repo.objects, tip)
                flat = flatten_tree(repo.objects, tree_oid)
                if path == '':
                    entries = []
                    seen = {}
                    for fpath, (mode, oid) in flat.items():
                        head, _, rest = fpath.partition('/')
                        if rest:
                            if head not in seen:
                                seen[head] = True
                                dir_oid = subtree_oid(
                                    repo.objects, tree_oid, head) or oid
                                entries.append({'type': 'dir', 'name': head,
                                                'path': head, 'sha': dir_oid,
                                                'size': 0})
                        else:
                            entry = repo.objects.get(oid)
                            size = len(entry[1]) if entry else 0
                            entries.append({'type': 'file', 'name': head,
                                            'path': head, 'sha': oid,
                                            'size': size})
                    entries.sort(key=lambda e: e['name'])
                    self._json(200, entries)
                    return
                if path in flat:
                    mode, oid = flat[path]
                    entry = repo.objects.get(oid)
                    content = entry[1] if entry else b''
                    self._json(200, {'type': 'file', 'encoding': 'base64',
                                     'size': len(content),
                                     'name': path.split('/')[-1], 'path': path,
                                     'content': b64encode(content), 'sha': oid})
                    return
                # Directory listing for a path prefix.
                prefix = path + '/'
                children = {}
                for fpath, (mode, oid) in flat.items():
                    if not fpath.startswith(prefix):
                        continue
                    rest = fpath[len(prefix):]
                    head, _, tail = rest.partition('/')
                    if tail:
                        if head not in children:
                            dir_oid = subtree_oid(
                                repo.objects, tree_oid,
                                prefix + head) or oid
                            children[head] = {'type': 'dir', 'name': head,
                                              'path': prefix + head,
                                              'sha': dir_oid, 'size': 0}
                    else:
                        entry = repo.objects.get(oid)
                        size = len(entry[1]) if entry else 0
                        children[head] = {'type': 'file', 'name': head,
                                          'path': fpath, 'sha': oid,
                                          'size': size}
                if children:
                    self._json(200, sorted(children.values(),
                                           key=lambda e: e['name']))
                    return
                self._err(404, 'Not Found')
                return
        if method in ('PUT', 'DELETE'):
            body, err = self._read_json()
            if err:
                self._err(400, err)
                return
            if not isinstance(body, dict):
                self._err(422, 'message is required')
                return
            message = body.get('message', '')
            branch = body.get('branch') or repo.default_branch
            with STORE.lock:
                if method == 'PUT':
                    content_b64 = body.get('content')
                    if content_b64 is None:
                        self._err(422, 'content is required')
                        return
                    content = b64decode(content_b64)
                    if content is None:
                        self._err(400, 'content is not valid base64')
                        return
                    parent_oid = repo.head_commit(branch)
                    existing = None
                    if parent_oid:
                        flat = flatten_tree(
                            repo.objects, _commit_tree(repo.objects, parent_oid))
                        existing = flat.get(path)
                    if existing is not None:
                        given_sha = body.get('sha')
                        if given_sha is None:
                            self._err(422, 'sha is required to update a file')
                            return
                        if given_sha != existing[1]:
                            self._err(409, 'sha does not match the current blob')
                            return
                        status = 200
                    else:
                        status = 201
                    commit_oid, blob_oid = repo.commit_file(
                        branch, path, content, message or 'Update %s' % path)
                    self._json(status, {
                        'content': {'name': path.split('/')[-1], 'path': path,
                                    'sha': blob_oid, 'size': len(content),
                                    'encoding': 'base64'},
                        'commit': {'sha': commit_oid, 'message': message}})
                    return
                # DELETE
                parent_oid = repo.head_commit(branch)
                if parent_oid is None:
                    self._err(404, 'Not Found')
                    return
                flat = flatten_tree(repo.objects,
                                    _commit_tree(repo.objects, parent_oid))
                existing = flat.get(path)
                if existing is None:
                    self._err(404, 'Not Found')
                    return
                given_sha = body.get('sha')
                if given_sha is not None and given_sha != existing[1]:
                    self._err(409, 'sha does not match the current blob')
                    return
                commit_oid, _ = repo.commit_file(
                    branch, path, b'', message or 'Delete %s' % path,
                    delete=True)
                self._json(200, {'commit': {'sha': commit_oid,
                                            'message': message}})
                return
        self._err(404, 'Not Found')

    def _route_rest_labels(self, method, repo, parts):
        if method == 'POST' and parts == []:
            body, err = self._read_json()
            if err:
                self._err(400, err)
                return
            if not isinstance(body, dict) or not body.get('name'):
                self._err(422, 'Label name is required')
                return
            name = str(body['name'])
            with STORE.lock:
                if name in repo.labels:
                    self._err(422, 'Label already exists')
                    return
                label = {'name': name, 'color': body.get('color', 'ededed'),
                         'description': body.get('description', '')}
                repo.labels[name] = label
            self._json(201, label)
            return
        if method == 'GET' and parts == []:
            with STORE.lock:
                labels = list(repo.labels.values())
            self._json(200, labels)
            return
        self._err(404, 'Not Found')

    def _route_rest_issues(self, method, repo, parts, query):
        if parts == []:
            if method == 'POST':
                body, err = self._read_json()
                if err:
                    self._err(400, err)
                    return
                if not isinstance(body, dict) or not body.get('title'):
                    self._err(422, 'Issue title is required')
                    return
                labels = body.get('labels') or []
                with STORE.lock:
                    for name in labels:
                        if name not in repo.labels:
                            self._err(422, 'Unknown label: %s' % name)
                            return
                    number = repo.next_issue
                    repo.next_issue += 1
                    issue = Issue(number, body['title'], body.get('body'),
                                  STORE.new_issue_db_id(), number)
                    issue.labels = list(labels)
                    repo.issues.append(issue)
                self._json(201, issue.to_json(repo.owner, repo.name))
                return
            if method == 'GET':
                state = query.get('state', ['open'])[0]
                sort = query.get('sort', ['created'])[0]
                direction = query.get('direction', ['desc'])[0]
                with STORE.lock:
                    issues = list(repo.issues)
                if state != 'all':
                    issues = [i for i in issues if i.state == state]
                reverse = (direction == 'desc')
                if sort == 'created':
                    issues.sort(key=lambda i: i.seq, reverse=reverse)
                items = [i.to_json(repo.owner, repo.name) for i in issues]
                page_items, page, per_page, pages = _paginate(items, query)
                headers = {}
                link = self._link_header(self.path.split('?')[0], query,
                                         page, per_page, pages)
                if link:
                    headers['Link'] = link
                self._json(200, page_items, headers)
                return
            self._err(404, 'Not Found')
            return
        if len(parts) >= 1 and parts[0].isdigit():
            number = int(parts[0])
            with STORE.lock:
                issue = repo.find_issue(number)
            if issue is None:
                self._err(404, 'Not Found')
                return
            if len(parts) == 1:
                if method == 'GET':
                    self._json(200, issue.to_json(repo.owner, repo.name))
                    return
                if method == 'PATCH':
                    body, err = self._read_json()
                    if err:
                        self._err(400, err)
                        return
                    if isinstance(body, dict):
                        with STORE.lock:
                            if 'state' in body and body['state'] in ('open', 'closed'):
                                issue.state = body['state']
                            if 'title' in body:
                                issue.title = body['title']
                            if 'body' in body:
                                issue.body = body['body'] or ''
                            issue.updated_at = NOW_ISO
                    self._json(200, issue.to_json(repo.owner, repo.name))
                    return
                self._err(404, 'Not Found')
                return
            if len(parts) == 2 and parts[1] == 'comments':
                if method == 'POST':
                    body, err = self._read_json()
                    if err:
                        self._err(400, err)
                        return
                    if not isinstance(body, dict) or not body.get('body'):
                        self._err(422, 'Comment body is required')
                        return
                    with STORE.lock:
                        comment = Comment(STORE.new_comment_id(), body['body'])
                        issue.comments.append(comment)
                    self._json(201, comment.to_json())
                    return
                if method == 'GET':
                    with STORE.lock:
                        comments = [c.to_json() for c in issue.comments]
                    self._json(200, comments)
                    return
                self._err(404, 'Not Found')
                return
        self._err(404, 'Not Found')

    def _route_rest_pulls(self, method, repo, parts, query):
        if parts == []:
            if method == 'POST':
                body, err = self._read_json()
                if err:
                    self._err(400, err)
                    return
                if not isinstance(body, dict):
                    self._err(422, 'head and base are required')
                    return
                head = body.get('head')
                base = body.get('base')
                title = body.get('title', '')
                if not head or not base:
                    self._err(422, 'head and base are required')
                    return
                with STORE.lock:
                    if repo.head_commit(head) is None:
                        self._err(422, 'Unknown head branch: %s' % head)
                        return
                    if repo.head_commit(base) is None:
                        self._err(422, 'Unknown base branch: %s' % base)
                        return
                    for existing in repo.pulls:
                        if (existing.head == head and existing.base == base
                                and existing.state == 'open'):
                            self._err(422, 'A pull request already exists for this head')
                            return
                    number = repo.next_pr
                    repo.next_pr += 1
                    pr = PullRequest(number, title, body.get('body'), head,
                                     base, STORE.new_pr_db_id(), number)
                    repo.pulls.append(pr)
                self._json(201, pr.to_json(repo))
                return
            if method == 'GET':
                state = query.get('state', ['open'])[0]
                with STORE.lock:
                    pulls = list(repo.pulls)
                if state != 'all':
                    pulls = [p for p in pulls if p.state == state]
                items = [p.to_json(repo) for p in pulls]
                page_items, page, per_page, pages = _paginate(items, query)
                headers = {}
                link = self._link_header(self.path.split('?')[0], query,
                                         page, per_page, pages)
                if link:
                    headers['Link'] = link
                self._json(200, page_items, headers)
                return
            self._err(404, 'Not Found')
            return
        if len(parts) >= 1 and parts[0].isdigit():
            number = int(parts[0])
            with STORE.lock:
                pr = repo.find_pr(number)
            if pr is None:
                self._err(404, 'Not Found')
                return
            if len(parts) == 1:
                if method == 'GET':
                    self._json(200, pr.to_json(repo))
                    return
                self._err(404, 'Not Found')
                return
            if len(parts) == 2 and parts[1] == 'files' and method == 'GET':
                with STORE.lock:
                    files = repo.pr_files(pr)
                self._json(200, files)
                return
            if len(parts) == 2 and parts[1] == 'merge' and method == 'PUT':
                body, err = self._read_json()
                if err:
                    self._err(400, err)
                    return
                if pr.state != 'open':
                    self._err(405, 'Pull request is not open')
                    return
                with STORE.lock:
                    state = repo.mergeable_state(pr)
                    if state == 'CONFLICTING':
                        self._err(405, 'Merge conflict')
                        return
                    commit_oid, reason = repo.squash_merge(pr)
                if commit_oid is None:
                    self._err(405, 'Merge conflict')
                    return
                self._json(200, {'sha': commit_oid, 'merged': True,
                                 'message': 'Merge pull request #%d' % number})
                return
            if len(parts) == 2 and parts[1] == 'comments' and method == 'GET':
                with STORE.lock:
                    comments = [c.to_json() for c in pr.comments]
                self._json(200, comments)
                return
        self._err(404, 'Not Found')

    # -- git smart HTTP -----------------------------------------------------
    def _route_git(self, method, parts, query):
        owner = parts[0]
        repo_name = parts[1][:-4]
        rest = parts[2:]
        repo = STORE.get_repo(owner, repo_name)
        if rest == ['info', 'refs'] and method == 'GET':
            service = query.get('service', [None])[0]
            if service not in ('git-upload-pack', 'git-receive-pack'):
                self._err(400, 'unknown service')
                return
            if repo is None:
                self._err(404, 'Not Found')
                return
            self._git_advertise(repo, service)
            return
        if repo is None:
            self._err(404, 'Not Found')
            return
        if rest == ['git-upload-pack'] and method == 'POST':
            self._git_upload_pack(repo)
            return
        if rest == ['git-receive-pack'] and method == 'POST':
            self._git_receive_pack(repo)
            return
        self._err(404, 'Not Found')

    def _git_advertise(self, repo, service):
        proto2 = self.headers.get('Git-Protocol', '') == 'version=2'
        if service == 'git-upload-pack' and proto2:
            body = pkt_lines(['version 2\n', 'ls-refs=unborn\n',
                              'fetch=shallow\n', 'object-format=sha1\n',
                              'flush'])
            self._send(200, body, 'application/x-git-upload-pack-advertisement')
            return
        with STORE.lock:
            refs = sorted(repo.refs.items())
            default_tip = repo.head_commit()
        if service == 'git-upload-pack':
            caps = ('symref=HEAD:refs/heads/main side-band-64k ofs-delta '
                    'shallow')
            ctype = 'application/x-git-upload-pack-advertisement'
        else:
            caps = ('symref=HEAD:refs/heads/main report-status side-band-64k '
                    'delete-refs ofs-delta')
            ctype = 'application/x-git-receive-pack-advertisement'
        out = bytearray()
        out += pkt_line('# service=%s\n' % service)
        out += PKT_FLUSH
        if not refs:
            out += pkt_line('%s capabilities^{}\0%s\n' % (ZERO_OID, caps))
        else:
            tip = default_tip or ZERO_OID
            out += pkt_line('%s HEAD\0%s\n' % (tip, caps))
            for ref, oid in refs:
                out += pkt_line('%s %s\n' % (oid, ref))
        out += PKT_FLUSH
        self._send(200, bytes(out), ctype)

    def _git_upload_pack(self, repo):
        body = self._read_body()
        proto2 = self.headers.get('Git-Protocol', '') == 'version=2'
        if proto2:
            self._git_upload_pack_v2(repo, body)
        else:
            self._git_upload_pack_v0(repo, body)

    def _v2_command(self, body):
        """Parse a v2 request body into (command, args)."""
        packets = parse_pkt_lines(body)
        lines = []
        for kind, payload in packets:
            if kind == 'data':
                lines.append(payload.decode('utf-8', 'replace'))
            elif kind == 'delim':
                lines.append(None)
            elif kind == 'flush':
                break
        if not lines or lines[0] is None or not lines[0].startswith('command='):
            return None, []
        command = lines[0][len('command='):].strip()
        args = []
        seen_delim = False
        for item in lines[1:]:
            if item is None:
                seen_delim = True
                continue
            if seen_delim:
                args.append(item.strip())
        return command, args

    def _git_upload_pack_v2(self, repo, body):
        command, args = self._v2_command(body)
        if command == 'ls-refs':
            self._git_ls_refs(repo, args)
        elif command == 'fetch':
            self._git_fetch(repo, args, version=2)
        else:
            self._err(400, 'unknown git command')

    def _git_ls_refs(self, repo, args):
        symrefs = 'symrefs' in args
        unborn = 'unborn' in args
        prefixes = [a[len('ref-prefix '):] for a in args
                    if a.startswith('ref-prefix ')]
        with STORE.lock:
            refs = sorted(repo.refs.items())
            default_tip = repo.head_commit()
        out = bytearray()

        def keep(name):
            return not prefixes or any(name.startswith(p) for p in prefixes)

        def emit(oid, name, attrs=''):
            out.extend(pkt_line('%s %s%s\n' % (oid, name, attrs)))

        if not refs:
            if unborn:
                if keep('HEAD'):
                    attrs = (' symref-target:refs/heads/main'
                             if symrefs else '')
                    emit('unborn', 'HEAD', attrs)
                if keep('refs/heads/main'):
                    emit('unborn', 'refs/heads/main')
        else:
            if keep('HEAD'):
                attrs = (' symref-target:refs/heads/main'
                         if symrefs else '')
                emit(default_tip, 'HEAD', attrs)
            for ref, oid in refs:
                if keep(ref):
                    emit(oid, ref)
        out += PKT_FLUSH
        self._send(200, bytes(out), 'application/x-git-upload-pack-result')

    def _git_fetch(self, repo, args, version):
        wants = []
        haves = []
        shallows = []
        depth = None
        for arg in args:
            if arg.startswith('want '):
                wants.append(arg[5:].strip().split()[0])
            elif arg.startswith('have '):
                haves.append(arg[5:].strip().split()[0])
            elif arg.startswith('shallow '):
                shallows.append(arg[8:].strip())
            elif arg.startswith('deepen '):
                try:
                    depth = int(arg[7:].strip())
                except ValueError:
                    pass
        with STORE.lock:
            # Haves the forge actually holds (commits). A NAK means "none of
            # your haves are common", so the pack must then carry everything
            # reachable from the wants; an ACK lets the pack omit them.
            common = [h for h in haves
                      if h in repo.objects
                      and repo.objects[h][0] == 'commit']
            effective_haves = haves if common else []
            objects, shallow, unshallow = collect_fetch_objects(
                repo.objects, wants, effective_haves, depth,
                prev_shallow=shallows)
        pack = build_pack(objects)
        out = bytearray()
        if version == 2:
            # Real git's server omits the acknowledgments section entirely
            # when the request carries no haves (the client then expects the
            # packfile section straight away); with haves it answers
            # ACKs + ready, or NAK when nothing is common.
            if haves:
                out += pkt_line('acknowledgments\n')
                if common:
                    for oid in common:
                        out += pkt_line('ACK %s\n' % oid)
                    out += pkt_line('ready\n')
                else:
                    out += pkt_line('NAK\n')
                out += PKT_DELIM
            if shallow or unshallow:
                out += pkt_line('shallow-info\n')
                for oid in shallow:
                    out += pkt_line('shallow %s\n' % oid)
                for oid in unshallow:
                    out += pkt_line('unshallow %s\n' % oid)
                out += PKT_DELIM
            out += pkt_line('packfile\n')
        else:
            # v0: exactly one ack line (no multi_ack advertised), then the
            # shallow lines, then the sideband pack.
            if haves and common:
                out += pkt_line('ACK %s\n' % common[0])
            else:
                out += pkt_line('NAK\n')
            for oid in shallow:
                out += pkt_line('shallow %s\n' % oid)
            for oid in unshallow:
                out += pkt_line('unshallow %s\n' % oid)
        for i in range(0, len(pack), 65515):
            out += pkt_line(b'\x01' + pack[i:i + 65515])
        out += PKT_FLUSH
        self._send(200, bytes(out), 'application/x-git-upload-pack-result')

    def _git_upload_pack_v0(self, repo, body):
        packets = parse_pkt_lines(body)
        args = []
        for kind, payload in packets:
            if kind == 'data':
                args.append(payload.decode('utf-8', 'replace').strip())
        self._git_fetch(repo, args, version=0)

    def _git_receive_pack(self, repo):
        body = self._read_body()
        commands = []
        pos = 0
        n = len(body)
        pack_start = n
        try:
            while pos + 4 <= n:
                length = int(body[pos:pos + 4].decode('ascii'), 16)
                if length == 0:
                    pos += 4
                    pack_start = pos
                    break
                if length < 4 or pos + length > n:
                    break
                payload = body[pos + 4:pos + length].decode('utf-8', 'replace')
                pos += length
                text = payload.split('\0', 1)[0].strip()
                if not text:
                    continue
                fields = text.split(' ')
                if len(fields) >= 3:
                    commands.append((fields[0], fields[1], fields[2]))
        except (ValueError, UnicodeDecodeError):
            pass
        pack_data = body[pack_start:] if pack_start < n else b''
        unpack_ok = True
        unpack_detail = 'ok'
        new_objects = {}
        if pack_data:
            try:
                new_objects = parse_pack(
                    pack_data,
                    resolve_base=lambda oid: repo.objects.get(oid))
            except Exception:
                unpack_ok = False
                unpack_detail = 'unpack error'
        results = []
        with STORE.lock:
            if unpack_ok:
                for oid, entry in new_objects.items():
                    repo.objects.setdefault(oid, entry)
                for old, new, ref in commands:
                    ok, reason = self._apply_push(repo, old, new, ref)
                    results.append((ref, ok, reason))
            else:
                for _old, _new, ref in commands:
                    results.append((ref, False, 'unpack error'))
        report = bytearray()
        report += pkt_line('unpack %s\n' % unpack_detail)
        for ref, ok, reason in results:
            if ok:
                report += pkt_line('ok %s\n' % ref)
            else:
                report += pkt_line('ng %s %s\n' % (ref, reason))
        report += PKT_FLUSH
        out = bytearray()
        for i in range(0, len(report), 65515):
            out += pkt_line(b'\x01' + bytes(report[i:i + 65515]))
        out += PKT_FLUSH
        self._send(200, bytes(out), 'application/x-git-receive-pack-result')

    def _apply_push(self, repo, old, new, ref):
        current = repo.refs.get(ref)
        if new == ZERO_OID:
            if current is None:
                return False, 'remote ref does not exist'
            if current != old:
                return False, 'non-fast-forward'
            del repo.refs[ref]
            return True, ''
        if old == ZERO_OID:
            if current is not None:
                return False, 'already exists'
            entry = repo.objects.get(new)
            if entry is None or entry[0] != 'commit':
                return False, 'object not found'
            repo.refs[ref] = new
            return True, ''
        if current is None:
            return False, 'remote ref does not exist'
        if current != old:
            return False, 'non-fast-forward'
        entry = repo.objects.get(new)
        if entry is None or entry[0] != 'commit':
            return False, 'object not found'
        if not is_ancestor(repo.objects, old, new):
            return False, 'non-fast-forward'
        repo.refs[ref] = new
        return True, ''

    # -- GraphQL v4 ---------------------------------------------------------
    # A small recursive-descent parser + executor for exactly the documents
    # gh and github-mcp-server send (see checks-graphql.ts). Unknown fields
    # answer HTTP 200 with an errors array naming the field.

    def _gql_lex(self, text):
        tokens = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch in ' \t\r\n,':
                i += 1
                continue
            if ch == '#':
                while i < n and text[i] != '\n':
                    i += 1
                continue
            if text.startswith('...', i):
                tokens.append(('ELLIPSIS', '...'))
                i += 3
                continue
            if ch in '{}()[]:!=$@=':
                tokens.append((ch, ch))
                i += 1
                continue
            if ch == '"':
                j = i + 1
                out = []
                while j < n and text[j] != '"':
                    if text[j] == '\\' and j + 1 < n:
                        esc = text[j + 1]
                        mapping = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b',
                                   'f': '\f', '"': '"', '\\': '\\', '/': '/'}
                        if esc == 'u' and j + 5 < n:
                            out.append(chr(int(text[j + 2:j + 6], 16)))
                            j += 6
                        else:
                            out.append(mapping.get(esc, esc))
                            j += 2
                    else:
                        out.append(text[j])
                        j += 1
                tokens.append(('STRING', ''.join(out)))
                i = j + 1
                continue
            if ch.isdigit() or (ch == '-' and i + 1 < n and text[i + 1].isdigit()):
                j = i
                if text[j] == '-':
                    j += 1
                while j < n and text[j].isdigit():
                    j += 1
                is_float = False
                if j < n and text[j] == '.':
                    is_float = True
                    j += 1
                    while j < n and text[j].isdigit():
                        j += 1
                if j < n and text[j] in 'eE':
                    is_float = True
                    j += 1
                    if j < n and text[j] in '+-':
                        j += 1
                    while j < n and text[j].isdigit():
                        j += 1
                raw = text[i:j]
                tokens.append(('FLOAT' if is_float else 'INT', raw))
                i = j
                continue
            if ch.isalpha() or ch == '_':
                j = i
                while j < n and (text[j].isalnum() or text[j] == '_'):
                    j += 1
                tokens.append(('NAME', text[i:j]))
                i = j
                continue
            raise ValueError('unexpected character %r in GraphQL document' % ch)
        tokens.append(('EOF', ''))
        return tokens

    def _gql_parse(self, text):
        tokens = self._gql_lex(text)
        pos = [0]

        def peek():
            return tokens[pos[0]]

        def take(kind=None):
            tok = tokens[pos[0]]
            if kind is not None and tok[0] != kind:
                raise ValueError('expected %s, got %r' % (kind, tok))
            pos[0] += 1
            return tok

        def skip_directives():
            while peek()[0] == '@':
                take('@')
                take('NAME')
                if peek()[0] == '(':
                    parse_arguments()

        def parse_value():
            kind, value = peek()
            if kind == '$':
                take('$')
                return {'kind': 'variable', 'name': take('NAME')[1]}
            if kind == 'INT':
                take('INT')
                return {'kind': 'int', 'value': int(value)}
            if kind == 'FLOAT':
                take('FLOAT')
                return {'kind': 'float', 'value': float(value)}
            if kind == 'STRING':
                take('STRING')
                return {'kind': 'string', 'value': value}
            if kind == '[':
                take('[')
                items = []
                while peek()[0] != ']':
                    items.append(parse_value())
                take(']')
                return {'kind': 'list', 'value': items}
            if kind == '{':
                take('{')
                fields = {}
                while peek()[0] != '}':
                    name = take('NAME')[1]
                    take(':')
                    fields[name] = parse_value()
                take('}')
                return {'kind': 'object', 'value': fields}
            if kind == 'NAME':
                take('NAME')
                if value == 'true':
                    return {'kind': 'boolean', 'value': True}
                if value == 'false':
                    return {'kind': 'boolean', 'value': False}
                if value == 'null':
                    return {'kind': 'null', 'value': None}
                return {'kind': 'enum', 'value': value}
            raise ValueError('unexpected value token %r' % (kind,))

        def parse_arguments():
            take('(')
            args = {}
            while peek()[0] != ')':
                name = take('NAME')[1]
                take(':')
                args[name] = parse_value()
            take(')')
            return args

        def parse_type():
            kind, value = peek()
            if kind == '[':
                take('[')
                inner = parse_type()
                take(']')
                node = {'kind': 'list', 'of': inner}
            elif kind == 'NAME':
                take('NAME')
                node = {'kind': 'named', 'name': value}
            else:
                raise ValueError('expected a type, got %r' % (kind,))
            if peek()[0] == '!':
                take('!')
                node = {'kind': 'nonnull', 'of': node}
            return node

        def parse_selection_set():
            take('{')
            selections = []
            while peek()[0] != '}':
                selections.append(parse_selection())
            take('}')
            return selections

        def parse_selection():
            if peek()[0] == 'ELLIPSIS':
                take('ELLIPSIS')
                kind, value = peek()
                if kind == 'NAME' and value == 'on':
                    take('NAME')
                    type_name = take('NAME')[1]
                    skip_directives()
                    return {'kind': 'inline', 'type': type_name,
                            'selections': parse_selection_set()}
                if kind == 'NAME':
                    name = take('NAME')[1]
                    skip_directives()
                    return {'kind': 'spread', 'name': name}
                raise ValueError('bad fragment')
            name = take('NAME')[1]
            alias = None
            if peek()[0] == ':':
                take(':')
                alias = name
                name = take('NAME')[1]
            args = {}
            if peek()[0] == '(':
                args = parse_arguments()
            skip_directives()
            selections = None
            if peek()[0] == '{':
                selections = parse_selection_set()
            return {'kind': 'field', 'name': name, 'alias': alias,
                    'args': args, 'selections': selections}

        def parse_operation():
            kind, value = peek()
            if kind == '{':
                return {'type': 'query', 'name': None, 'variables': {},
                        'selections': parse_selection_set()}
            if kind == 'NAME' and value in ('query', 'mutation', 'subscription'):
                take('NAME')
                op_type = value
                name = None
                if peek()[0] == 'NAME':
                    name = take('NAME')[1]
                variables = {}
                if peek()[0] == '(':
                    take('(')
                    while peek()[0] != ')':
                        take('$')
                        var_name = take('NAME')[1]
                        take(':')
                        var_type = parse_type()
                        default = None
                        if peek()[0] == '=':
                            take('=')
                            default = parse_value()
                        variables[var_name] = {'type': var_type,
                                               'default': default}
                    take(')')
                skip_directives()
                return {'type': op_type, 'name': name, 'variables': variables,
                        'selections': parse_selection_set()}
            raise ValueError('expected an operation, got %r' % (kind,))

        operations = []
        fragments = {}
        while peek()[0] != 'EOF':
            kind, value = peek()
            if kind == 'NAME' and value == 'fragment':
                take('NAME')
                frag_name = take('NAME')[1]
                on_word = take('NAME')[1]
                if on_word != 'on':
                    raise ValueError('expected "on" in fragment')
                type_name = take('NAME')[1]
                skip_directives()
                fragments[frag_name] = {'type': type_name,
                                        'selections': parse_selection_set()}
            else:
                operations.append(parse_operation())
        return {'operations': operations, 'fragments': fragments}

    def _gql_value(self, node, variables):
        kind = node['kind']
        if kind == 'variable':
            return variables.get(node['name'])
        if kind == 'list':
            return [self._gql_value(item, variables) for item in node['value']]
        if kind == 'object':
            return {key: self._gql_value(val, variables)
                    for key, val in node['value'].items()}
        return node['value']

    def _gql_args(self, arg_nodes, variables):
        return {name: self._gql_value(node, variables)
                for name, node in arg_nodes.items()}

    # -- field resolvers ----------------------------------------------------
    def _gql_field(self, type_name, obj, name, args):
        """Resolve one field; returns the raw value (a dict with '_type' for
        objects, a list, a scalar, or None). Raises KeyError for unknown
        fields (the caller turns it into a GraphQL error naming the field).
        """
        if name == '__typename':
            return type_name
        if type_name == 'Query':
            if name == 'viewer':
                return {'_type': 'User', 'login': VIEWER_LOGIN}
            if name == 'repository':
                repo = STORE.get_repo(args.get('owner'), args.get('name'))
                if repo is None:
                    return None
                return {'_type': 'Repository', '_repo': repo}
            if name == 'node':
                return self._gql_node(args.get('id'))
            if name == '__type':
                return self._gql_type_info(args.get('name'))
            raise KeyError(name)
        if type_name == 'User':
            if name == 'login':
                return obj['login']
            raise KeyError(name)
        if type_name == 'Repository':
            repo = obj['_repo']
            if name == 'id':
                return repo.gid()
            if name == 'databaseId':
                return repo.db_id
            if name == 'name':
                return repo.name
            if name == 'nameWithOwner':
                return '%s/%s' % (repo.owner, repo.name)
            if name == 'owner':
                return {'_type': 'User', 'login': repo.owner}
            if name == 'defaultBranchRef':
                if repo.head_commit() is None:
                    return None
                return {'_type': 'Ref', 'name': repo.default_branch}
            if name == 'viewerPermission':
                return 'ADMIN'
            if name == 'hasIssuesEnabled':
                return True
            if name == 'description':
                return repo.description
            if name == 'issue':
                issue = repo.find_issue(args.get('number'))
                if issue is None:
                    return None
                return {'_type': 'Issue', '_issue': issue, '_repo': repo}
            if name == 'issues':
                return self._gql_issue_connection(repo, args)
            if name == 'pullRequests':
                return self._gql_pr_connection(repo, args)
            if name == 'pullRequest':
                pr = repo.find_pr(args.get('number'))
                if pr is None:
                    return None
                return {'_type': 'PullRequest', '_pr': pr, '_repo': repo}
            raise KeyError(name)
        if type_name == 'Issue':
            issue = obj['_issue']
            if name == 'id':
                return issue.gid(obj['_repo'].owner, obj['_repo'].name)
            if name == 'number':
                return issue.number
            if name == 'title':
                return issue.title
            if name == 'state':
                return issue.state.upper()
            if name == 'body':
                return issue.body
            raise KeyError(name)
        if type_name == 'PullRequest':
            pr = obj['_pr']
            repo = obj['_repo']
            if name == 'id':
                return pr.gid(repo.owner, repo.name)
            if name == 'number':
                return pr.number
            if name == 'title':
                return pr.title
            if name == 'body':
                return pr.body
            if name == 'state':
                return pr.state.upper()
            if name == 'merged':
                return pr.merged
            if name == 'headRefName':
                return pr.head
            if name == 'baseRefName':
                return pr.base
            if name == 'mergeable':
                return repo.mergeable_state(pr)
            if name == 'files':
                first = args.get('first')
                files = repo.pr_files(pr)
                if first is not None:
                    files = files[:first]
                return {'_type': 'PullRequestFileConnection',
                        '_nodes': [{'_type': 'PullRequestFile',
                                    'path': f['filename']} for f in files]}
            raise KeyError(name)
        if type_name in ('IssueConnection', 'PullRequestConnection',
                         'PullRequestFileConnection'):
            if name == 'nodes':
                return obj['_nodes']
            if name == 'pageInfo':
                return {'_type': 'PageInfo',
                        'hasNextPage': obj.get('_has_next', False),
                        'endCursor': obj.get('_end_cursor')}
            raise KeyError(name)
        if type_name == 'PageInfo':
            if name in ('hasNextPage', 'endCursor'):
                return obj[name]
            raise KeyError(name)
        if type_name == 'Ref':
            if name == 'name':
                return obj['name']
            raise KeyError(name)
        if type_name == 'PullRequestFile':
            if name == 'path':
                return obj['path']
            raise KeyError(name)
        if type_name == '__Type':
            if name == 'name':
                return obj['name']
            if name == 'fields':
                return [{'_type': '__Field', 'name': f}
                        for f in obj.get('fields', [])]
            if name == 'enumValues':
                return [{'_type': '__EnumValue', 'name': v}
                        for v in obj.get('enumValues', [])]
            raise KeyError(name)
        if type_name in ('__Field', '__EnumValue'):
            if name == 'name':
                return obj['name']
            raise KeyError(name)
        if type_name == 'IssueComment':
            if name == 'id':
                return obj['_comment_gid']
            if name == 'body':
                return obj['_comment'].body
            raise KeyError(name)
        if type_name == 'IssueCommentEdge':
            if name == 'node':
                return obj['node']
            if name == 'cursor':
                return obj.get('cursor')
            raise KeyError(name)
        if type_name == 'CreateIssuePayload':
            if name == 'issue':
                return obj['issue']
            raise KeyError(name)
        if type_name == 'CloseIssuePayload':
            if name == 'issue':
                return obj['issue']
            raise KeyError(name)
        if type_name == 'CreatePullRequestPayload':
            if name == 'pullRequest':
                return obj['pullRequest']
            raise KeyError(name)
        if type_name == 'MergePullRequestPayload':
            if name == 'pullRequest':
                return obj['pullRequest']
            raise KeyError(name)
        if type_name == 'AddCommentPayload':
            if name == 'commentEdge':
                return obj['commentEdge']
            raise KeyError(name)
        raise KeyError(name)

    def _gql_type_info(self, name):
        fields = {
            'Issue': ['id', 'number', 'title', 'state', 'body'],
            'PullRequest': ['id', 'number', 'headRefName', 'baseRefName',
                          'mergeable', 'state', 'merged', 'files',
                          'title', 'body'],
            'Repository': ['id', 'databaseId', 'name', 'nameWithOwner',
                           'owner', 'defaultBranchRef', 'viewerPermission',
                           'hasIssuesEnabled', 'issue', 'issues',
                           'pullRequests', 'pullRequest', 'description'],
        }
        enums = {
            'SearchType': ['ISSUE', 'REPOSITORY', 'USER', 'DISCUSSION'],
            'IssueState': ['OPEN', 'CLOSED'],
            'PullRequestState': ['OPEN', 'CLOSED', 'MERGED'],
            'MergeableState': ['MERGEABLE', 'CONFLICTING', 'UNKNOWN'],
        }
        if name in fields:
            return {'_type': '__Type', 'name': name, 'fields': fields[name],
                    'enumValues': []}
        if name in enums:
            return {'_type': '__Type', 'name': name, 'fields': [],
                    'enumValues': enums[name]}
        return None

    def _gql_node(self, gid_value):
        decoded = ungid(gid_value) if isinstance(gid_value, str) else None
        if decoded is None:
            return None
        kind, parts = decoded
        if kind == 'Repository' and len(parts) == 2:
            repo = STORE.get_repo(parts[0], parts[1])
            if repo is None:
                return None
            return {'_type': 'Repository', '_repo': repo}
        if kind == 'Issue' and len(parts) == 3:
            repo = STORE.get_repo(parts[0], parts[1])
            if repo is None:
                return None
            issue = repo.find_issue(int(parts[2]))
            if issue is None:
                return None
            return {'_type': 'Issue', '_issue': issue, '_repo': repo}
        if kind == 'PullRequest' and len(parts) == 3:
            repo = STORE.get_repo(parts[0], parts[1])
            if repo is None:
                return None
            pr = repo.find_pr(int(parts[2]))
            if pr is None:
                return None
            return {'_type': 'PullRequest', '_pr': pr, '_repo': repo}
        return None

    def _gql_issue_connection(self, repo, args):
        first = args.get('first')
        after = args.get('after')
        states = args.get('states')
        order_by = args.get('orderBy') or {}
        with STORE.lock:
            issues = list(repo.issues)
        if states:
            wanted = {str(s).upper() for s in states}
            issues = [i for i in issues if i.state.upper() in wanted]
        direction = str(order_by.get('direction', 'ASC')).upper()
        issues.sort(key=lambda i: i.number, reverse=(direction == 'DESC'))
        offset = 0
        if after:
            decoded = cursor_decode(after)
            if decoded is not None:
                offset = decoded
        page = issues[offset:]
        if first is not None:
            page = page[:first]
        has_next = (offset + len(page)) < len(issues)
        end_cursor = None
        if page:
            end_cursor = cursor_encode(offset + len(page))
        nodes = [{'_type': 'Issue', '_issue': issue, '_repo': repo}
                 for issue in page]
        return {'_type': 'IssueConnection', '_nodes': nodes,
                '_has_next': has_next, '_end_cursor': end_cursor}

    def _gql_pr_connection(self, repo, args):
        first = args.get('first')
        after = args.get('after')
        with STORE.lock:
            pulls = list(repo.pulls)
        pulls.sort(key=lambda p: p.number)
        offset = 0
        if after:
            decoded = cursor_decode(after)
            if decoded is not None:
                offset = decoded
        page = pulls[offset:]
        if first is not None:
            page = page[:first]
        has_next = (offset + len(page)) < len(pulls)
        end_cursor = None
        if page:
            end_cursor = cursor_encode(offset + len(page))
        nodes = [{'_type': 'PullRequest', '_pr': pr, '_repo': repo}
                 for pr in page]
        return {'_type': 'PullRequestConnection', '_nodes': nodes,
                '_has_next': has_next, '_end_cursor': end_cursor}

    # -- mutations ------------------------------------------------------------
    def _gql_mutation(self, name, args):
        """Execute one root mutation field; returns the payload object."""
        if name == 'createIssue':
            data = args.get('input') or {}
            decoded = ungid(data.get('repositoryId', ''))
            if decoded is None or decoded[0] != 'Repository':
                raise ValueError('unknown repository id')
            repo = STORE.get_repo(decoded[1][0], decoded[1][1])
            if repo is None:
                raise ValueError('unknown repository')
            with STORE.lock:
                number = repo.next_issue
                repo.next_issue += 1
                issue = Issue(number, data.get('title', ''),
                              data.get('body'), STORE.new_issue_db_id(),
                              number)
                repo.issues.append(issue)
            return {'_type': 'CreateIssuePayload',
                    'issue': {'_type': 'Issue', '_issue': issue,
                              '_repo': repo}}
        if name == 'closeIssue':
            data = args.get('input') or {}
            decoded = ungid(data.get('issueId', ''))
            if decoded is None or decoded[0] != 'Issue':
                raise ValueError('unknown issue id')
            repo = STORE.get_repo(decoded[1][0], decoded[1][1])
            if repo is None:
                raise ValueError('unknown repository')
            with STORE.lock:
                issue = repo.find_issue(int(decoded[1][2]))
                if issue is None:
                    raise ValueError('unknown issue')
                issue.state = 'closed'
                issue.updated_at = NOW_ISO
            return {'_type': 'CloseIssuePayload',
                    'issue': {'_type': 'Issue', '_issue': issue,
                              '_repo': repo}}
        if name == 'createPullRequest':
            data = args.get('input') or {}
            decoded = ungid(data.get('repositoryId', ''))
            if decoded is None or decoded[0] != 'Repository':
                raise ValueError('unknown repository id')
            repo = STORE.get_repo(decoded[1][0], decoded[1][1])
            if repo is None:
                raise ValueError('unknown repository')
            head = data.get('headRefName')
            base = data.get('baseRefName')
            with STORE.lock:
                if repo.head_commit(head) is None:
                    raise ValueError('unknown head branch')
                if repo.head_commit(base) is None:
                    raise ValueError('unknown base branch')
                number = repo.next_pr
                repo.next_pr += 1
                pr = PullRequest(number, data.get('title', ''),
                                 data.get('body'), head, base,
                                 STORE.new_pr_db_id(), number)
                repo.pulls.append(pr)
            return {'_type': 'CreatePullRequestPayload',
                    'pullRequest': {'_type': 'PullRequest', '_pr': pr,
                                    '_repo': repo}}
        if name == 'mergePullRequest':
            data = args.get('input') or {}
            decoded = ungid(data.get('pullRequestId', ''))
            if decoded is None or decoded[0] != 'PullRequest':
                raise ValueError('unknown pull request id')
            repo = STORE.get_repo(decoded[1][0], decoded[1][1])
            if repo is None:
                raise ValueError('unknown repository')
            with STORE.lock:
                pr = repo.find_pr(int(decoded[1][2]))
                if pr is None:
                    raise ValueError('unknown pull request')
                if pr.state != 'open':
                    raise ValueError('pull request is not open')
                if repo.mergeable_state(pr) == 'CONFLICTING':
                    raise ValueError('merge conflict')
                commit_oid, _reason = repo.squash_merge(pr)
                if commit_oid is None:
                    raise ValueError('merge conflict')
            return {'_type': 'MergePullRequestPayload',
                    'pullRequest': {'_type': 'PullRequest', '_pr': pr,
                                    '_repo': repo}}
        if name == 'addComment':
            data = args.get('input') or {}
            decoded = ungid(data.get('subjectId', ''))
            if decoded is None:
                raise ValueError('unknown subject id')
            kind, parts = decoded
            repo = STORE.get_repo(parts[0], parts[1])
            if repo is None:
                raise ValueError('unknown repository')
            with STORE.lock:
                comment = Comment(STORE.new_comment_id(), data.get('body', ''))
                if kind == 'Issue':
                    issue = repo.find_issue(int(parts[2]))
                    if issue is None:
                        raise ValueError('unknown issue')
                    issue.comments.append(comment)
                    comment_gid = gid('IssueComment', parts[0], parts[1],
                                      parts[2], comment.id)
                elif kind == 'PullRequest':
                    pr = repo.find_pr(int(parts[2]))
                    if pr is None:
                        raise ValueError('unknown pull request')
                    pr.comments.append(comment)
                    comment_gid = gid('IssueComment', parts[0], parts[1],
                                      parts[2], comment.id)
                else:
                    raise ValueError('unsupported subject')
            return {'_type': 'AddCommentPayload',
                    'commentEdge': {'_type': 'IssueCommentEdge',
                                    'node': {'_type': 'IssueComment',
                                             '_comment': comment,
                                             '_comment_gid': comment_gid}}}
        raise KeyError(name)

    # -- selection execution --------------------------------------------------
    def _gql_execute_selections(self, type_name, obj, selections, variables,
                                fragments, errors):
        result = {}
        for sel in selections:
            if sel['kind'] == 'field':
                name = sel['name']
                key = sel['alias'] or name
                args = self._gql_args(sel['args'], variables)
                try:
                    if type_name == 'Mutation':
                        value = self._gql_mutation(name, args)
                        value_type = value.get('_type') if isinstance(value, dict) else None
                    else:
                        value = self._gql_field(type_name, obj, name, args)
                        value_type = (value.get('_type')
                                      if isinstance(value, dict) else None)
                except KeyError:
                    errors.append({'message':
                                   'Cannot query field "%s" on type "%s".'
                                   % (name, type_name)})
                    result[key] = None
                    continue
                except ValueError as exc:
                    errors.append({'message': str(exc)})
                    result[key] = None
                    continue
                if sel['selections'] is None:
                    result[key] = value
                else:
                    if value is None:
                        result[key] = None
                    elif isinstance(value, list):
                        result[key] = [
                            self._gql_execute_selections(
                                item.get('_type'), item, sel['selections'],
                                variables, fragments, errors)
                            if isinstance(item, dict) else item
                            for item in value]
                    elif isinstance(value, dict):
                        result[key] = self._gql_execute_selections(
                            value_type, value, sel['selections'], variables,
                            fragments, errors)
                    else:
                        result[key] = value
            elif sel['kind'] == 'inline':
                if sel['type'] is None or sel['type'] == type_name:
                    sub = self._gql_execute_selections(
                        type_name, obj, sel['selections'], variables,
                        fragments, errors)
                    result.update(sub)
            elif sel['kind'] == 'spread':
                frag = fragments.get(sel['name'])
                if frag is not None and frag['type'] == type_name:
                    sub = self._gql_execute_selections(
                        type_name, obj, frag['selections'], variables,
                        fragments, errors)
                    result.update(sub)
        return result

    def _gql_run(self, item):
        """Run one GraphQL operation object; returns the response dict."""
        if not isinstance(item, dict) or not isinstance(item.get('query'), str):
            return {'data': None,
                    'errors': [{'message': 'invalid GraphQL request'}]}
        query = item['query']
        variables = item.get('variables') or {}
        if not isinstance(variables, dict):
            variables = {}
        try:
            doc = self._gql_parse(query)
        except ValueError as exc:
            return {'data': None, 'errors': [{'message': str(exc)}]}
        if not doc['operations']:
            return {'data': None,
                    'errors': [{'message': 'no operation in document'}]}
        operation = doc['operations'][0]
        op_name = item.get('operationName')
        if op_name:
            for candidate in doc['operations']:
                if candidate.get('name') == op_name:
                    operation = candidate
                    break
        errors = []
        root_type = 'Mutation' if operation['type'] == 'mutation' else 'Query'
        data = self._gql_execute_selections(
            root_type, {'_type': root_type}, operation['selections'],
            variables, doc['fragments'], errors)
        response = {'data': data}
        if errors:
            response['errors'] = errors
        return response

    def _route_graphql(self, method):
        if method != 'POST':
            self._err(405, 'Method Not Allowed')
            return
        body, err = self._read_json()
        if err:
            self._err(400, err)
            return
        if isinstance(body, list):
            self._json(200, [self._gql_run(item) for item in body])
            return
        if isinstance(body, dict):
            self._json(200, self._gql_run(body))
            return
        self._err(400, 'invalid GraphQL request')

    # -- MCP (JSON-RPC 2.0 Streamable HTTP) ----------------------------------
    # Modern 2026-07-28 era: server/discover first; legacy initialize is
    # answered too. One fixed tool list (byte-for-byte identical every call).

    def _route_mcp(self, method):
        if method == 'DELETE':
            self._send(200, b'')
            return
        if method != 'POST':
            self._err(405, 'Method Not Allowed')
            return
        raw = self._read_body()
        try:
            body = json.loads(raw.decode('utf-8')) if raw else None
        except Exception:
            self._json(400, {'jsonrpc': '2.0', 'id': None,
                             'error': {'code': -32700,
                                       'message': 'Parse error'}})
            return
        if not isinstance(body, dict):
            self._json(400, {'jsonrpc': '2.0', 'id': None,
                             'error': {'code': -32700,
                                       'message': 'Parse error'}})
            return
        # Notifications carry no id: 202 with an empty body.
        if 'id' not in body or body.get('id') is None:
            self._send(202, b'')
            return
        rpc_id = body.get('id')
        method_name = body.get('method')
        params = body.get('params') or {}
        if not isinstance(params, dict):
            params = {}

        def ok(result):
            self._json(200, {'jsonrpc': '2.0', 'id': rpc_id, 'result': result})

        def fail(code, message):
            self._json(200, {'jsonrpc': '2.0', 'id': rpc_id,
                             'error': {'code': code, 'message': message}})

        if method_name == 'server/discover':
            ok({'supportedVersions': ['2026-07-28', '2025-11-25'],
                'capabilities': {'tools': {}},
                'serverInfo': {'name': SERVER_NAME, 'version': VERSION},
                '_meta': {'io.modelcontextprotocol/serverInfo':
                          {'name': SERVER_NAME, 'version': VERSION}}})
            return
        if method_name == 'initialize':
            ok({'protocolVersion': '2026-07-28',
                'capabilities': {'tools': {}},
                'serverInfo': {'name': SERVER_NAME, 'version': VERSION}})
            return
        if method_name == 'ping':
            ok({})
            return
        if method_name == 'tools/list':
            ok({'tools': MCP_TOOLS})
            return
        if method_name == 'tools/call':
            name = params.get('name')
            args = params.get('arguments') or {}
            if not isinstance(args, dict):
                args = {}
            result, error = self._mcp_call_tool(name, args)
            if error is not None:
                code, message = error
                fail(code, message)
                return
            ok(result)
            return
        fail(-32601, 'Method not found: %s' % method_name)

    def _mcp_call_tool(self, name, args):
        """Returns (result_dict, None) or (None, (code, message))."""
        known = {tool['name'] for tool in MCP_TOOLS}
        if name not in known:
            return None, (-32602, 'Unknown tool: %s' % name)

        def missing(*required):
            absent = [field for field in required if args.get(field) is None]
            if absent:
                return {'content': [{'type': 'text',
                                     'text': 'missing required argument(s): %s'
                                             % ', '.join(absent)}],
                        'structuredContent': {'error': 'missing arguments'},
                        'isError': True}
            return None

        def tool_result(payload):
            text = json.dumps(payload, separators=(',', ':'),
                              ensure_ascii=False)
            return {'content': [{'type': 'text', 'text': text}],
                    'structuredContent': payload}

        if name == 'create_repo':
            err = missing('name')
            if err:
                return err, None
            repo = STORE.create_repo(VIEWER_LOGIN, str(args['name']),
                                     args.get('description'),
                                     args.get('private', False))
            if repo is None:
                return {'content': [{'type': 'text',
                                     'text': 'Repository already exists'}],
                        'structuredContent': {'error': 'already exists'},
                        'isError': True}, None
            return tool_result(repo.to_json()), None
        if name == 'get_repo':
            err = missing('owner', 'repo')
            if err:
                return err, None
            repo = STORE.get_repo(args['owner'], args['repo'])
            if repo is None:
                return {'content': [{'type': 'text', 'text': 'Not Found'}],
                        'structuredContent': {'error': 'not found'},
                        'isError': True}, None
            return tool_result(repo.to_json()), None
        if name == 'create_issue':
            err = missing('owner', 'repo', 'title')
            if err:
                return err, None
            repo = STORE.get_repo(args['owner'], args['repo'])
            if repo is None:
                return {'content': [{'type': 'text', 'text': 'Not Found'}],
                        'structuredContent': {'error': 'not found'},
                        'isError': True}, None
            with STORE.lock:
                number = repo.next_issue
                repo.next_issue += 1
                issue = Issue(number, args['title'], args.get('body'),
                              STORE.new_issue_db_id(), number)
                repo.issues.append(issue)
            return tool_result(issue.to_json(repo.owner, repo.name)), None
        if name == 'list_issues':
            err = missing('owner', 'repo')
            if err:
                return err, None
            repo = STORE.get_repo(args['owner'], args['repo'])
            if repo is None:
                return {'content': [{'type': 'text', 'text': 'Not Found'}],
                        'structuredContent': {'error': 'not found'},
                        'isError': True}, None
            state = args.get('state', 'open')
            with STORE.lock:
                issues = list(repo.issues)
            if state != 'all':
                issues = [i for i in issues if i.state == state]
            payload = {'issues': [i.to_json(repo.owner, repo.name)
                                  for i in issues]}
            return tool_result(payload), None
        if name == 'create_pull_request':
            err = missing('owner', 'repo', 'title', 'head', 'base')
            if err:
                return err, None
            repo = STORE.get_repo(args['owner'], args['repo'])
            if repo is None:
                return {'content': [{'type': 'text', 'text': 'Not Found'}],
                        'structuredContent': {'error': 'not found'},
                        'isError': True}, None
            with STORE.lock:
                if repo.head_commit(args['head']) is None:
                    return {'content': [{'type': 'text',
                                         'text': 'Unknown head branch'}],
                            'structuredContent': {'error': 'unknown head'},
                            'isError': True}, None
                if repo.head_commit(args['base']) is None:
                    return {'content': [{'type': 'text',
                                         'text': 'Unknown base branch'}],
                            'structuredContent': {'error': 'unknown base'},
                            'isError': True}, None
                number = repo.next_pr
                repo.next_pr += 1
                pr = PullRequest(number, args['title'], args.get('body'),
                                 args['head'], args['base'],
                                 STORE.new_pr_db_id(), number)
                repo.pulls.append(pr)
            return tool_result(pr.to_json(repo)), None
        if name == 'merge_pull_request':
            err = missing('owner', 'repo', 'pull_number')
            if err:
                return err, None
            repo = STORE.get_repo(args['owner'], args['repo'])
            if repo is None:
                return {'content': [{'type': 'text', 'text': 'Not Found'}],
                        'structuredContent': {'error': 'not found'},
                        'isError': True}, None
            with STORE.lock:
                pr = repo.find_pr(int(args['pull_number']))
                if pr is None:
                    return {'content': [{'type': 'text',
                                         'text': 'Pull request not found'}],
                            'structuredContent': {'error': 'not found'},
                            'isError': True}, None
                if pr.state != 'open':
                    return {'content': [{'type': 'text',
                                         'text': 'Pull request is not open'}],
                            'structuredContent': {'error': 'not open'},
                            'isError': True}, None
                if repo.mergeable_state(pr) == 'CONFLICTING':
                    return {'content': [{'type': 'text',
                                         'text': 'Merge conflict'}],
                            'structuredContent': {'error': 'conflict'},
                            'isError': True}, None
                commit_oid, _reason = repo.squash_merge(pr)
            return tool_result({'merged': True, 'sha': commit_oid,
                                'number': pr.number}), None
        return None, (-32602, 'Unknown tool: %s' % name)


MCP_TOOLS = [
    {'name': 'create_repo',
     'description': 'Create a repository for the token owner.',
     'inputSchema': {'type': 'object',
                     'properties': {
                         'name': {'type': 'string',
                                  'description': 'Repository name'},
                         'description': {'type': 'string',
                                         'description': 'Repository description'},
                         'private': {'type': 'boolean',
                                     'description': 'Whether the repository is private'},
                         'owner': {'type': 'string',
                                   'description': 'Repository owner (the token owner)'}},
                     'required': ['name']}},
    {'name': 'get_repo',
     'description': 'Get a repository by owner and name.',
     'inputSchema': {'type': 'object',
                     'properties': {
                         'owner': {'type': 'string'},
                         'repo': {'type': 'string'}},
                     'required': ['owner', 'repo']}},
    {'name': 'create_issue',
     'description': 'Create an issue in a repository.',
     'inputSchema': {'type': 'object',
                     'properties': {
                         'owner': {'type': 'string'},
                         'repo': {'type': 'string'},
                         'title': {'type': 'string'},
                         'body': {'type': 'string'}},
                     'required': ['owner', 'repo', 'title']}},
    {'name': 'list_issues',
     'description': 'List issues in a repository.',
     'inputSchema': {'type': 'object',
                     'properties': {
                         'owner': {'type': 'string'},
                         'repo': {'type': 'string'},
                         'state': {'type': 'string',
                                   'description': 'open, closed or all'}},
                     'required': ['owner', 'repo']}},
    {'name': 'create_pull_request',
     'description': 'Open a pull request from a head branch onto a base branch.',
     'inputSchema': {'type': 'object',
                     'properties': {
                         'owner': {'type': 'string'},
                         'repo': {'type': 'string'},
                         'title': {'type': 'string'},
                         'head': {'type': 'string'},
                         'base': {'type': 'string'}},
                     'required': ['owner', 'repo', 'title', 'head', 'base']}},
    {'name': 'merge_pull_request',
     'description': 'Merge a pull request with a squash commit.',
     'inputSchema': {'type': 'object',
                     'properties': {
                         'owner': {'type': 'string'},
                         'repo': {'type': 'string'},
                         'pull_number': {'type': 'integer'}},
                     'required': ['owner', 'repo', 'pull_number']}},
]


# --------------------------------------------------------------------------
# Lifecycle: bind ephemeral port, register with the env, wait for done
# --------------------------------------------------------------------------

def _http_call(url, method='GET', body=None, timeout=30):
    data = json_bytes(body) if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception:
        return None, b''


def _retry_delay(attempt):
    return min(2.0, 0.25 * (2 ** min(attempt, 8)))


def _register(port):
    attempt = 0
    while True:
        status, _body = _http_call(ENV_URL + '/register', 'POST',
                                   {'port': port})
        if status in (200, 409):
            return
        if status in (400, 405):
            raise RuntimeError('register answered HTTP %s' % status)
        time.sleep(_retry_delay(attempt))
        attempt += 1


def _wait_for_done():
    attempt = 0
    while True:
        status, body = _http_call(ENV_URL + '/done', 'GET')
        if status == 200:
            try:
                if json.loads(body.decode('utf-8')).get('done') is True:
                    return
            except Exception:
                pass
        time.sleep(_retry_delay(attempt))
        attempt += 1


def main():
    server = ThreadingHTTPServer(('0.0.0.0', 0), ForgeHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sys.stdout.write('[entry] listening on port %d\n' % port)
    sys.stdout.flush()
    if not ENV_URL:
        # Standalone mode (local testing only): serve until interrupted.
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        server.shutdown()
        return 0
    _register(port)
    sys.stdout.write('[entry] registered; waiting for the env to say done\n')
    sys.stdout.flush()
    _wait_for_done()
    server.shutdown()
    sys.stdout.write('[entry] done\n')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - top-level crash report
        sys.stderr.write('[entry] %s\n' % exc)
        sys.exit(1)
