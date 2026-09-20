#!/usr/bin/env python3
"""Structural and install-time validation of this marketplace.

check-jsonschema (CI) proves the documents are well formed. This script proves
they are installable, which is a stronger claim: ops-notifier shipped for a
while as a perfectly well-formed, perfectly well-signed bundle whose module was
labelled wasm@1, while the gateway only accepts jobs@1 on a module a job
references. Every install failed manifest validation, and no schema caught it
because the marketplace's own schema pinned the wrong label too.

So this is the install-time dry run, mirroring internal/plugin and
internal/pluginrt:

  * the runtime whitelist, and the role binding that ties a job to a jobs@1
    module and a provider to a wasm@2 one;
  * the exports each ABI's entrypoint requires, and the imports the host can
    link, both read out of the module bytes, so a module built against the wrong
    SDK — or one importing a host the gateway never instantiates — fails here
    instead of trapping on its first run;
  * the size bound the runtime enforces, and the content digest;
  * index/manifest agreement, so a bundle nobody can fetch, or a bundle the
    index does not name, fails here rather than 404-ing for a user;
  * that no signing key has been revoked out from under a listed package.

Signatures are verified separately, in Go, by scripts/verify_signatures.go —
the signature covers the gateway's canonical bytes, and Go's encoder is what
defines them. `make check` runs both.
"""
import base64
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The closed runtime vocabulary, mirroring internal/plugin.RuntimeJobs and
# RuntimeWASMv2. A module labelled anything else is refused at install time.
RUNTIME_JOBS = "jobs@1"
RUNTIME_WASM_V2 = "wasm@2"
RUNTIMES = {RUNTIME_JOBS, RUNTIME_WASM_V2}

# The exports each ABI's entrypoint contract requires, mirroring the shape
# checks in internal/pluginrt: openInstance for wasm@2, invoke for jobs@1.
REQUIRED_EXPORTS = {
    RUNTIME_JOBS: ("memory", "alloc", "run"),
    RUNTIME_WASM_V2: ("memory", "alloc", "handle"),
}

# The only import surfaces the host instantiates: the ABI's one host function,
# beside WASI (Go and TinyGo builds import clocks, randomness and stderr). A
# module importing anything else can never instantiate — the host has no such
# module to link — so it is an install that is guaranteed to fail at first run,
# which is worth catching at publish time instead.
ABI_IMPORT = {
    RUNTIME_JOBS: "ai_proxy_v1",
    RUNTIME_WASM_V2: "ai_proxy_v2",
}
WASI_IMPORTS = ("wasi_snapshot_preview1", "wasi_unstable")

# internal/plugin.settingKeyRe: module ids and job keys share this shape.
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# internal/plugin.MaxJobTimeout, and the 8 MiB module ceiling the gateway and
# the build script both enforce.
MAX_JOB_TIMEOUT_NS = 2 * 60 * 1_000_000_000
MAX_MODULE_BYTES = 8 << 20

# Go's time.ParseDuration units, which is what the scheduler parses a job
# timeout with. Anything else is an install-time rejection.
GO_UNITS_NS = {
    "ns": 1,
    "us": 1_000,
    "\u00b5s": 1_000,
    "\u03bcs": 1_000,
    "ms": 1_000_000,
    "s": 1_000_000_000,
    "m": 60_000_000_000,
    "h": 3_600_000_000_000,
}
GO_DURATION_RE = re.compile(r"([0-9]*\.?[0-9]+)(ns|us|\u00b5s|\u03bcs|ms|s|m|h)")


def fail(message):
    raise SystemExit(f"validate: {message}")


def load(path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def go_duration_ns(text):
    """Parse a Go duration, or None. Rejects the empty string and trailing
    garbage the way time.ParseDuration does."""
    if not text:
        return None
    total, position, matched = 0.0, 0, False
    while position < len(text):
        m = GO_DURATION_RE.match(text, position)
        if not m:
            return None
        total += float(m.group(1)) * GO_UNITS_NS[m.group(2)]
        position = m.end()
        matched = True
    return total if matched else None


def read_uleb(raw, pos):
    result, shift = 0, 0
    while True:
        if pos >= len(raw):
            raise ValueError("truncated unsigned LEB128")
        byte = raw[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def wasm_imports(raw):
    """The (module, name) pairs a wasm module imports.

    Reads the import section only: enough to answer "can this host link it at
    all", which is decided before any guest code runs and is the other half of
    the same question wasm_exports answers."""
    if not raw.startswith(b"\0asm"):
        raise ValueError("not a WebAssembly module")
    pos, imports = 8, []
    while pos < len(raw):
        section_id, pos = read_uleb(raw, pos)
        size, pos = read_uleb(raw, pos)
        end = pos + size
        if section_id == 2:  # import section
            count, cur = read_uleb(raw, pos)
            for _ in range(count):
                length, cur = read_uleb(raw, cur)
                module = raw[cur:cur + length].decode("utf-8", "replace")
                cur += length
                length, cur = read_uleb(raw, cur)
                name = raw[cur:cur + length].decode("utf-8", "replace")
                cur += length
                kind = raw[cur]
                cur += 1
                if kind == 0:  # function: type index
                    _, cur = read_uleb(raw, cur)
                elif kind == 1:  # table: element type, then limits
                    cur += 1
                    cur = skip_limits(raw, cur)
                elif kind == 2:  # memory: limits
                    cur = skip_limits(raw, cur)
                elif kind == 3:  # global: valtype, mutability
                    cur += 2
                else:
                    raise ValueError(f"unknown import kind {kind}")
                imports.append((module, name))
        pos = end
    return imports


def skip_limits(raw, pos):
    """Skip a wasm limits encoding: a flags byte, a min, and a max when flagged."""
    flags = raw[pos]
    pos += 1
    _, pos = read_uleb(raw, pos)
    if flags & 0x01:
        _, pos = read_uleb(raw, pos)
    return pos


def wasm_exports(raw):
    """The export names of a wasm module, read from its export section only.

    Enough to answer "does this module export the entrypoint its runtime
    promises" without a wasm runtime, which is the question that decides
    whether an install succeeds and a first run traps."""
    if not raw.startswith(b"\0asm"):
        raise ValueError("not a WebAssembly module")
    pos, names = 8, set()
    while pos < len(raw):
        section_id, pos = read_uleb(raw, pos)
        size, pos = read_uleb(raw, pos)
        end = pos + size
        if section_id == 7:  # export section
            count, cur = read_uleb(raw, pos)
            for _ in range(count):
                length, cur = read_uleb(raw, cur)
                names.add(raw[cur:cur + length].decode("utf-8", "replace"))
                cur += length + 1  # name, then the export kind
                _, cur = read_uleb(raw, cur)  # then the export index
        pos = end
    return names


index = load(ROOT / "index.json")
entries = index["plugins"]
by_id = {entry["id"]: entry for entry in entries}
if len(by_id) != len(entries):
    fail("index.json contains duplicate ids")

publishers = {}
for path in sorted((ROOT / "publishers").glob("*.json")):
    doc = load(path)
    if doc["handle"] != path.stem:
        fail(f"{path}: handle does not match filename")
    keys = {}
    for key in doc["keys"]:
        raw = base64.b64decode(key["public_key"], validate=True)
        if len(raw) != 32:
            fail(f"{path}: Ed25519 public key must be 32 bytes")
        key_id = "ed25519:" + raw[:8].hex()
        if key["key_id"] != key_id:
            fail(f"{path}: key_id does not match public_key")
        keys[key_id] = key
    publishers[doc["handle"]] = keys

revoke = load(ROOT / "revoke.json")
if revoke.get("version") != 1 or not isinstance(revoke.get("revoked"), list):
    fail("invalid revoke.json")
revoked_keys = {item["key_id"] for item in revoke["revoked"] if item.get("key_id")}

# The gateway fetches <base>/plugins/<id>.json (internal/plugin/market.go
# fetchManifest) and the keys from <base>/publishers/<handle>.json. A bundle
# that is not at the path the gateway asks for is invisible no matter how valid
# it is, so the file set is checked against the index rather than against
# itself — which is how automations/ came to hold a second, stale copy of a
# signed bundle that nothing ever fetched.
files = {path.stem: path for path in sorted((ROOT / "plugins").glob("*.json"))}
missing = sorted(set(by_id) - set(files))
if missing:
    fail(f"index names bundles with no file in plugins/: {', '.join(missing)}")
extra = sorted(set(files) - set(by_id))
if extra:
    fail(f"plugins/ holds bundles the index does not name: {', '.join(extra)}")

checked = 0
for automation_id, path in files.items():
    manifest = load(path)
    entry = by_id[automation_id]
    if manifest["id"] != automation_id:
        fail(f"{path}: id does not match filename")
    for field in ("version", "type", "name", "description", "publisher"):
        if entry.get(field) != manifest.get(field):
            fail(f"{path}: index {field} differs from manifest")
    if manifest["type"] != "automation":
        fail(f"{path}: this marketplace only publishes automation packages")

    signature = manifest.get("signature")
    if not signature:
        fail(f"{path}: manifest is unsigned; a marketplace bundle must be signed by its publisher")
    key_id = signature.get("key_id")
    if key_id not in publishers.get(manifest["publisher"], {}):
        fail(f"{path}: signing key {key_id} is not published under {manifest['publisher']}")
    if entry.get("key_id") != key_id:
        fail(f"{path}: index key_id differs from the manifest's")
    if key_id in revoked_keys:
        fail(f"{path}: signed with revoked key {key_id}")

    # ─── the install-time dry run ───
    modules = {}
    for module in manifest["modules"]:
        module_id = module["id"]
        if not KEY_RE.match(module_id):
            fail(f"{path}: module id {module_id!r} must be lowercase letters, digits and underscores")
        if module_id in modules:
            fail(f"{path}: modules declares {module_id!r} twice")
        raw = base64.b64decode(module["data"], validate=True)
        if not raw.startswith(b"\0asm"):
            fail(f"{path}: {module_id} is not WebAssembly")
        if len(raw) > MAX_MODULE_BYTES:
            fail(f"{path}: {module_id} is {len(raw)} bytes, over the {MAX_MODULE_BYTES} ceiling")
        if hashlib.sha256(raw).hexdigest() != module["sha256"]:
            fail(f"{path}: {module_id} sha256 differs")
        runtime = module["runtime"]
        if runtime not in RUNTIMES:
            fail(f"{path}: module {module_id!r} runtime must be {RUNTIME_JOBS} or {RUNTIME_WASM_V2}, not {runtime!r}")
        try:
            exports = wasm_exports(raw)
        except ValueError as err:
            fail(f"{path}: {module_id} is unreadable ({err})")
        absent = [name for name in REQUIRED_EXPORTS[runtime] if name not in exports]
        if absent:
            fail(f"{path}: module {module_id!r} claims runtime {runtime} but exports no {', '.join(absent)}")
        try:
            imports = wasm_imports(raw)
        except ValueError as err:
            fail(f"{path}: {module_id} is unreadable ({err})")
        for host, name in imports:
            if host == ABI_IMPORT[runtime] and name == "call":
                continue
            if host in WASI_IMPORTS:
                continue
            fail(f"{path}: module {module_id!r} imports {host}.{name}, which the host does not provide; "
                 f"{runtime} modules may import {ABI_IMPORT[runtime]}.call and WASI only")
        modules[module_id] = module

    used = set()
    for job in manifest["provides"]["jobs"]:
        if not KEY_RE.match(job["key"]):
            fail(f"{path}: job key {job['key']!r} must be lowercase letters, digits and underscores")
        module = modules.get(job["module"])
        if module is None:
            fail(f"{path}: job {job['key']!r} names a missing module")
        if module["runtime"] != RUNTIME_JOBS:
            fail(f"{path}: job {job['key']!r} names module {job['module']!r} with runtime "
                 f"{module['runtime']!r}; jobs run the {RUNTIME_JOBS} ABI — rebuild the bundle with the current SDK")
        used.add(job["module"])
        if not job.get("schedule"):
            fail(f"{path}: job {job['key']!r} has no schedule")
        if job.get("timeout"):
            ns = go_duration_ns(job["timeout"])
            if ns is None:
                fail(f"{path}: job {job['key']!r} has an unparseable timeout {job['timeout']!r}")
            if ns <= 0 or ns > MAX_JOB_TIMEOUT_NS:
                fail(f"{path}: job {job['key']!r} timeout must be between 0 and 2m")
    for provider in manifest["provides"].get("providers", []):
        module = modules.get(provider["module"])
        if module is None:
            fail(f"{path}: provider names a missing module {provider['module']!r}")
        if module["runtime"] != RUNTIME_WASM_V2:
            fail(f"{path}: provider names module {provider['module']!r} with runtime "
                 f"{module['runtime']!r}; provider modules run the {RUNTIME_WASM_V2} ABI")
        used.add(provider["module"])
    orphans = sorted(set(modules) - used)
    if orphans:
        fail(f"{path}: module(s) no job or provider uses: {', '.join(orphans)}")
    checked += 1

print(f"validated {checked} automation manifests against the gateway's install rules")

