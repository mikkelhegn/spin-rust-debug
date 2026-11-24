#!/usr/bin/env python3
"""Reusable LLDB helper for debugging Spin HTTP components written in Rust.

Copy this file to your project and load it with LLDB:
    command script import /path/to/spin_debug.py

Provides spin-debug-target, spin-debug-start, spin-debug-stop, and wasm-headers.
Launching expects a Spin `debug` profile and listens on 127.0.0.1:3107.
Requires LLDB with Python support and Spin on PATH; no pip packages or test
fixtures are needed. The process launcher uses POSIX process groups and ps.

Formatters adapted from Radu Matei's wasi-http-lldb-debugging.py gist:
https://gist.github.com/radu-matei/8f9f41ec251d6db995970dcdd7f6f349
Revision aa60e4f780e9fde6f1f42be7f1561265a1527b77.
HTTP field layouts/header spellings: https://github.com/hyperium/http/tree/v1.5.0/src
Guest memory reads use Wasmtime's DWARF field types and enum metadata.
"""

import atexit
from functools import wraps
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import time


# Spin process lifecycle


_spin = None
_supervisor = None
_child_pid = None
_log_path = None


def _cleanup(debugger=None):
    global _supervisor, _child_pid
    if _supervisor is None:
        return

    # Kill the attached trigger before waiting for the supervisor, including
    # when the trigger is suspended at a breakpoint.
    if debugger is not None and _child_pid is not None:
        process = debugger.GetSelectedTarget().GetProcess()
        if process.IsValid() and process.GetProcessID() == _child_pid:
            process.Kill()

    # The new session belongs exclusively to the Spin process we launched.
    # Only signal it while its leader is still alive, avoiding stale PID reuse.
    if _supervisor.poll() is None:
        try:
            os.killpg(_supervisor.pid, signal.SIGTERM)
            _supervisor.wait(timeout=2)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(_supervisor.pid, signal.SIGKILL)
            _supervisor.wait(timeout=2)
    _supervisor = None
    _child_pid = None


def _http_child(parent_pid):
    output = subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,command="], text=True
    )
    prefix = f"{_spin} trigger http"
    matches = []
    for line in output.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3 or int(fields[1]) != parent_pid:
            continue
        if fields[2] == prefix or fields[2].startswith(prefix + " "):
            matches.append(int(fields[0]))
    if len(matches) > 1:
        raise RuntimeError("Expected one HTTP trigger child for this Spin launch")
    return matches[0] if matches else None


def target(debugger, command, result, internal_dict):
    """Create the target from the Spin executable on PATH."""
    global _spin
    spin = shutil.which("spin")
    if spin is None:
        result.SetError("Cannot find spin on PATH. Start VS Code from your shell.")
        return
    _spin = str(Path(spin).resolve())
    if not debugger.CreateTarget(_spin).IsValid():
        result.SetError(f"Could not create LLDB target for {_spin}")


def start(debugger, command, result, internal_dict):
    """Start the debug profile and attach only to its own HTTP trigger."""
    global _supervisor, _child_pid, _log_path
    try:
        args = shlex.split(command)
        if len(args) != 1:
            raise RuntimeError('Usage: spin-debug-start "<workspace folder>"')
        if _spin is None:
            raise RuntimeError("Run spin-debug-target first")
        _cleanup(debugger)
        workspace = Path(args[0]).resolve()
        _log_path = workspace / ".spin" / "debug.log"
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        with _log_path.open("w") as log:
            _supervisor = subprocess.Popen(
                [
                    _spin, "up", "--profile", "debug", "--debug-info",
                    "--listen", "127.0.0.1:3107",
                ],
                cwd=workspace,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _supervisor.poll() is not None:
                raise RuntimeError(f"Spin exited with code {_supervisor.returncode}")
            _child_pid = _http_child(_supervisor.pid)
            if _child_pid and "Available Routes:" in _log_path.read_text(errors="replace"):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Timed out waiting for the Spin HTTP trigger")

        error = lldb.SBError()
        process = debugger.GetSelectedTarget().AttachToProcessWithID(
            debugger.GetListener(), _child_pid, error
        )
        if error.Fail() or not process.IsValid():
            raise RuntimeError(f"Cannot attach to Spin HTTP trigger: {error}")
        result.AppendMessage(
            f"Attached to Spin HTTP trigger {_child_pid}. "
            f"Request: http://127.0.0.1:3107\nSpin output: {_log_path}"
        )
    except Exception as error:
        _cleanup(debugger)
        log = _log_path.read_text(errors="replace")[-6000:] if _log_path and _log_path.exists() else ""
        result.SetError(f"{error}\n{log}")


def stop(debugger, command, result, internal_dict):
    """Stop only the Spin process tree created by this debugging session."""
    _cleanup(debugger)


# Guest memory access and Rust values


MAX_BYTES = 4096
MAX_ITEMS = 256
CATEGORY = "wasm-rust"


class Unavailable(Exception):
    """Unsupported layout, unavailable debug value, or bounded read failure."""


def raw(value):
    value = value.GetNonSyntheticValue()
    if not value.IsValid() or value.GetError().Fail():
        raise Unavailable("value is unavailable")
    return value


def child(value, *names):
    value = raw(value)
    for name in names:
        value = raw(value.GetChildMemberWithName(name))
    return value


def uint(value):
    error = lldb.SBError()
    number = raw(value).GetValueAsUnsigned(error, 0)
    if error.Fail():
        raise Unavailable("integer is unavailable")
    return number


def memory_base(value):
    # Never borrow another component's __vmctx from a different stack frame.
    frame = raw(value).GetFrame()
    if not frame.IsValid():
        raise Unavailable("no Wasm frame")
    vmctx = raw(frame.FindVariable("__vmctx"))
    if vmctx.GetTypeName() != "WasmtimeVMContext *":
        raise Unavailable("not a Wasmtime guest frame")
    base = uint(child(vmctx.Dereference(), "memory"))
    if not base:
        raise Unavailable("no linear memory in debug metadata")
    return base


def pointer_offset(value):
    value = raw(value)
    if not (value.GetTypeName() or "").startswith(
        ("WebAssemblyPtrWrapper<", "WebAssemblyRefWrapper<")
    ):
        raise Unavailable("not a Wasm pointer wrapper")
    return uint(child(value, "__ptr"))


def read_memory(value, offset, length):
    if length < 0 or length > MAX_BYTES:
        raise Unavailable(f"read exceeds {MAX_BYTES} bytes")
    if offset < 0 or offset + length > 2**32:
        raise Unavailable("Wasm32 address overflow")
    base = memory_base(value)
    if length == 0:
        return b""
    error = lldb.SBError()
    data = value.GetProcess().ReadMemory(base + offset, length, error)
    if error.Fail() or data is None or len(data) != length:
        raise Unavailable("linear memory is unreadable")
    return bytes(data)


def deref(value):
    value = raw(value)
    offset = pointer_offset(value)
    typ = value.GetType().GetTemplateArgumentType(0)
    if not typ.IsValid():
        raise Unavailable("missing pointer element type")
    read_memory(value, offset, typ.GetByteSize())
    return raw(value.CreateValueFromAddress("*" + (value.GetName() or "ptr"),
                                           memory_base(value) + offset, typ))


def enum(value):
    """Decode LLDB's Clang-encoded Rust enum, including explicit niche defaults.

    The $variant$N member names encode discriminants in debug metadata. N is
    not a source-order index. Mirrors Rust's lldb_providers.py without guessing
    a variant when there is no matching discriminant or explicit default.
    """
    value = raw(value)
    variants = value.GetChildMemberWithName("$variants$").GetNonSyntheticValue()
    if not variants.IsValid():
        members = value.GetType().GetEnumMembers()
        number = uint(value)
        for i in range(members.GetSize()):
            member = members.GetTypeEnumMemberAtIndex(i)
            if member.GetValueAsUnsigned() == number:
                return member.GetName().rsplit("::", 1)[-1], value
        raise Unavailable("unknown enum layout")
    default = None
    for i in range(variants.GetNumChildren()):
        variant = raw(variants.GetChildAtIndex(i))
        discr = variant.GetChildMemberWithName("$discr$")
        if discr.IsValid():
            if variant.GetName() != f"$variant${uint(discr)}":
                continue
        elif variant.GetName() == "$variant$":
            default = variant
            continue
        else:
            raise Unavailable("unsupported enum discriminator")
        return _variant(variant)
    if default is not None:
        return _variant(default)
    raise Unavailable("invalid enum discriminator")


def _variant(value):
    name = value.GetTypeName().rsplit("::", 1)[-1]
    if not name.endswith("$Variant"):
        raise Unavailable("missing enum variant name")
    return name[:-len("$Variant")], child(value, "value")


def vec_parts(value):
    value = raw(value)
    if not (value.GetTypeName() or "").startswith("alloc::vec::Vec<"):
        raise Unavailable("not a Vec")
    pointer = child(value, "buf", "inner", "ptr", "pointer", "pointer")
    offset = pointer_offset(pointer)
    length = uint(child(value, "len"))
    capacity = uint(child(value, "buf", "inner", "cap", "__0"))
    typ = value.GetType().GetTemplateArgumentType(0)
    if not typ.IsValid() or (typ.GetByteSize() > 0 and length > capacity):
        raise Unavailable("invalid vector metadata")
    return offset, length, typ


def vec_items(value):
    value = raw(value)
    offset, length, typ = vec_parts(value)
    return array_items(value, offset, length, typ)


def array_items(value, offset, length, typ):
    if length > MAX_ITEMS:
        raise Unavailable(f"collection exceeds {MAX_ITEMS} elements")
    size = typ.GetByteSize()
    if offset + length * size > 2**32:
        raise Unavailable("vector address overflow")
    base = memory_base(value)
    items = []
    for i in range(length):
        read_memory(value, offset + i * size, size)
        items.append(raw(value.CreateValueFromAddress(f"[{i}]", base + offset + i * size, typ)))
    return items


def read_bytes(value):
    value = raw(value)
    name = value.GetTypeName() or ""
    if name == "alloc::string::String":
        return read_bytes(child(value, "vec"))
    if name == "http::byte_str::ByteStr":
        return read_bytes(child(value, "bytes"))
    if name.startswith("alloc::vec::Vec<"):
        offset, length, typ = vec_parts(value)
        if typ.GetName() not in ("u8", "unsigned char"):
            raise Unavailable("not a byte vector")
    elif name == "bytes::bytes::Bytes":
        offset = pointer_offset(child(value, "ptr"))
        length = uint(child(value, "len"))
    else:
        # str, slices and Box<[u8]> use DWARF fat-pointer fields.
        pointer = child(value, "data_ptr")
        typ = pointer.GetType().GetTemplateArgumentType(0)
        if typ.GetName() not in ("u8", "unsigned char"):
            raise Unavailable("not a byte slice")
        offset = pointer_offset(pointer)
        length = uint(child(value, "length"))
    return read_memory(value, offset, length)


def quote(data):
    try:
        return json.dumps(data.decode("utf-8"), ensure_ascii=False)
    except UnicodeDecodeError:
        # Preserve invalid UTF-8 rather than silently replacing bytes.
        return repr(data)


def unavailable_summary(error):
    # LLDB renders Python None as the literal text "None", which looks like a
    # successfully decoded Rust value. Always make failed decoding explicit.
    reason = str(error) if isinstance(error, Unavailable) else "unsupported debug layout"
    return f"<unavailable: {reason}; inspect raw fields>"


def bytes_summary(value, _):
    try:
        return quote(read_bytes(value))
    except (Unavailable, AttributeError, ValueError) as error:
        return unavailable_summary(error)


def vec_summary(value, _):
    try:
        offset, length, typ = vec_parts(value)
        memory_base(value)
        if typ.GetName() in ("u8", "unsigned char"):
            return quote(read_memory(value, offset, length))
        suffix = f" (expand limit {MAX_ITEMS})" if length > MAX_ITEMS else ""
        return f"size={length}{suffix}"
    except (Unavailable, AttributeError, ValueError) as error:
        return unavailable_summary(error)


class VecSynthetic:
    def __init__(self, value, _):
        self.value = value.GetNonSyntheticValue()
        self.update()

    def update(self):
        # LLDB can reapply a provider to its returned child even when that
        # child's prefer-synthetic flag is false. Terminate the raw view here.
        if self.value.GetName() == "[raw]":
            self.children = [self.value.GetChildAtIndex(i)
                             for i in range(self.value.GetNumChildren())]
            return False
        try:
            self.children = self.items()
        except (Unavailable, AttributeError, ValueError):
            self.children = []
        raw_child = self.value.Clone("[raw]")
        raw_child.SetPreferSyntheticValue(False)
        self.children.append(raw_child)
        return False

    def items(self):
        return vec_items(self.value)

    def num_children(self):
        return len(self.children)

    def has_children(self):
        return True

    def get_child_index(self, name):
        return next((i for i, v in enumerate(self.children) if v.GetName() == name), -1)

    def get_child_at_index(self, index):
        return self.children[index] if 0 <= index < len(self.children) else None


class ByteBufferSynthetic(VecSynthetic):
    """Replace native Rust pointer dereferencing when expanding guest strings."""

    def items(self):
        value = self.value
        name = value.GetTypeName()
        if name == "alloc::string::String":
            return vec_items(child(value, "vec"))
        if name == "bytes::bytes::Bytes":
            pointer, length = child(value, "ptr"), uint(child(value, "len"))
        else:
            pointer, length = child(value, "data_ptr"), uint(child(value, "length"))
        typ = pointer.GetType().GetTemplateArgumentType(0)
        if typ.GetName() not in ("u8", "unsigned char"):
            raise Unavailable("not a byte buffer")
        return array_items(value, pointer_offset(pointer), length, typ)


def headers_command(debugger, command, result, _):
    try:
        path = command.strip() or "req.head.headers"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(\.[A-Za-z_][A-Za-z_0-9]*)*", path):
            raise Unavailable("use a variable and optional .fields, e.g. req.head.headers")
        names = path.split(".")
        value = debugger.GetSelectedTarget().GetProcess().GetSelectedThread().GetSelectedFrame().FindVariable(names[0])
        value = child(value, *names[1:])
        for name, header in header_items(value):
            summary = header_value_summary(header, {})
            result.AppendMessage(f"{name.decode('ascii')}: {summary}")
    except (Unavailable, AttributeError, ValueError) as error:
        result.SetError(str(error))


# HTTP summaries and header expansion


def _summary(callback):
    """Flag unavailable summaries; underlying fields remain inspectable."""
    @wraps(callback)
    def safe(value, _dict=None, _options=None):
        try:
            return callback(raw(value))
        except Exception as error:
            return unavailable_summary(error)
    return safe


_METHODS = {
    "Options": b"OPTIONS", "Get": b"GET", "Post": b"POST", "Put": b"PUT",
    "Delete": b"DELETE", "Head": b"HEAD", "Trace": b"TRACE", "Connect": b"CONNECT",
    "Patch": b"PATCH", "Query": b"QUERY",
}


_STANDARD_HEADERS = {
    'Accept': 'accept',
    'AcceptCharset': 'accept-charset',
    'AcceptEncoding': 'accept-encoding',
    'AcceptLanguage': 'accept-language',
    'AcceptRanges': 'accept-ranges',
    'AccessControlAllowCredentials': 'access-control-allow-credentials',
    'AccessControlAllowHeaders': 'access-control-allow-headers',
    'AccessControlAllowMethods': 'access-control-allow-methods',
    'AccessControlAllowOrigin': 'access-control-allow-origin',
    'AccessControlExposeHeaders': 'access-control-expose-headers',
    'AccessControlMaxAge': 'access-control-max-age',
    'AccessControlRequestHeaders': 'access-control-request-headers',
    'AccessControlRequestMethod': 'access-control-request-method',
    'Age': 'age',
    'Allow': 'allow',
    'AltSvc': 'alt-svc',
    'Authorization': 'authorization',
    'CacheControl': 'cache-control',
    'CacheStatus': 'cache-status',
    'CdnCacheControl': 'cdn-cache-control',
    'Connection': 'connection',
    'ContentDisposition': 'content-disposition',
    'ContentEncoding': 'content-encoding',
    'ContentLanguage': 'content-language',
    'ContentLength': 'content-length',
    'ContentLocation': 'content-location',
    'ContentRange': 'content-range',
    'ContentSecurityPolicy': 'content-security-policy',
    'ContentSecurityPolicyReportOnly': 'content-security-policy-report-only',
    'ContentType': 'content-type',
    'Cookie': 'cookie',
    'Dnt': 'dnt',
    'Date': 'date',
    'Etag': 'etag',
    'Expect': 'expect',
    'Expires': 'expires',
    'Forwarded': 'forwarded',
    'From': 'from',
    'Host': 'host',
    'IfMatch': 'if-match',
    'IfModifiedSince': 'if-modified-since',
    'IfNoneMatch': 'if-none-match',
    'IfRange': 'if-range',
    'IfUnmodifiedSince': 'if-unmodified-since',
    'LastModified': 'last-modified',
    'Link': 'link',
    'Location': 'location',
    'MaxForwards': 'max-forwards',
    'Origin': 'origin',
    'Pragma': 'pragma',
    'ProxyAuthenticate': 'proxy-authenticate',
    'ProxyAuthorization': 'proxy-authorization',
    'PublicKeyPins': 'public-key-pins',
    'PublicKeyPinsReportOnly': 'public-key-pins-report-only',
    'Range': 'range',
    'Referer': 'referer',
    'ReferrerPolicy': 'referrer-policy',
    'Refresh': 'refresh',
    'RetryAfter': 'retry-after',
    'SecWebSocketAccept': 'sec-websocket-accept',
    'SecWebSocketExtensions': 'sec-websocket-extensions',
    'SecWebSocketKey': 'sec-websocket-key',
    'SecWebSocketProtocol': 'sec-websocket-protocol',
    'SecWebSocketVersion': 'sec-websocket-version',
    'Server': 'server',
    'SetCookie': 'set-cookie',
    'StrictTransportSecurity': 'strict-transport-security',
    'Te': 'te',
    'Trailer': 'trailer',
    'TransferEncoding': 'transfer-encoding',
    'UserAgent': 'user-agent',
    'Upgrade': 'upgrade',
    'UpgradeInsecureRequests': 'upgrade-insecure-requests',
    'Vary': 'vary',
    'Via': 'via',
    'Warning': 'warning',
    'WwwAuthenticate': 'www-authenticate',
    'XContentTypeOptions': 'x-content-type-options',
    'XDnsPrefetchControl': 'x-dns-prefetch-control',
    'XFrameOptions': 'x-frame-options',
    'XXssProtection': 'x-xss-protection',
}


def method_bytes(value):
    variant, payload = enum(child(value, "__0"))
    if variant in _METHODS:
        return _METHODS[variant]
    extension = child(payload, "__0")
    if variant == "ExtensionInline":
        storage = child(extension, "__0")
        length = uint(child(extension, "__1"))
        if length > storage.GetNumChildren():
            raise Unavailable("invalid inline HTTP method length")
        return bytes(uint(storage.GetChildAtIndex(index)) for index in range(length))
    if variant == "ExtensionAllocated":
        return read_bytes(child(extension, "__0"))
    raise Unavailable("unsupported HTTP method")


@_summary
def method_summary(value):
    return method_bytes(value).decode("ascii")


def scheme_bytes(value):
    variant, payload = enum(child(value, "inner"))
    if variant == "None":
        return b""
    if variant == "Standard":
        protocol, _ = enum(child(payload, "__0"))
        return {"Http": b"http", "Https": b"https"}[protocol]
    if variant == "Other":
        return read_bytes(deref(child(payload, "__0")))
    raise Unavailable("unsupported URI scheme")


@_summary
def scheme_summary(value):
    scheme = scheme_bytes(value)
    return quote(scheme) if scheme else "(no scheme)"


@_summary
def authority_summary(value):
    return quote(read_bytes(child(value, "data")))


@_summary
def path_and_query_summary(value):
    return quote(read_bytes(child(value, "data")) or b"/")


def uri_bytes(value):
    scheme = scheme_bytes(child(value, "scheme"))
    authority = read_bytes(child(value, "authority", "data"))
    path = child(value, "path_and_query")
    data = read_bytes(child(path, "data"))
    query = uint(child(path, "query"))
    if query == 65535:
        path_data, query_data = data, b""
    else:
        if query >= len(data) or data[query:query + 1] != b"?":
            raise Unavailable("invalid URI query index")
        path_data, query_data = data[:query], data[query:]
    # Match Uri::path()/has_path(): CONNECT's authority-only target has no '/'.
    if not path_data and (data or scheme):
        path_data = b"/"
    prefix = scheme + b"://" if scheme else b""
    return prefix + authority + path_data + query_data


@_summary
def uri_summary(value):
    return quote(uri_bytes(value))


def header_name_bytes(value):
    variant, payload = enum(child(value, "inner"))
    if variant == "Standard":
        name, _ = enum(child(payload, "__0"))
        return _STANDARD_HEADERS[name].encode("ascii")
    if variant == "Custom":
        return read_bytes(child(payload, "__0", "__0"))
    raise Unavailable("unsupported HTTP header name")


@_summary
def header_name_summary(value):
    return quote(header_name_bytes(value))


@_summary
def header_value_summary(value):
    if uint(child(value, "is_sensitive")):
        return "(sensitive)"
    return quote(read_bytes(child(value, "inner")))


def header_counts(value):
    names = uint(child(value, "entries", "len"))
    extras = uint(child(value, "extra_values", "len"))
    return names, names + extras


@_summary
def header_map_summary(value):
    names, count = header_counts(value)
    return "HeaderMap (%d values, %d names)" % (count, names)


def header_items(value):
    """Return every (name, value) in http's documented multimap order.

    The extra-value vectors are dense. Validate the links as well as their
    bounds, so partial/corrupt state cannot produce plausibly wrong headers.
    """
    name_count, count = header_counts(value)
    if count > 256:
        raise Unavailable("header map exceeds the 256-value display limit")
    entries = vec_items(child(value, "entries"))
    extras = vec_items(child(value, "extra_values"))
    if len(entries) != name_count or len(extras) != count - name_count:
        raise Unavailable("header vectors were truncated")
    result, visited = [], set()
    for entry_index, entry in enumerate(entries):
        name = header_name_bytes(child(entry, "key"))
        result.append((name, child(entry, "value")))
        variant, payload = enum(child(entry, "links"))
        if variant == "None":
            continue
        if variant != "Some":
            raise Unavailable("unsupported header links")
        links = child(payload, "__0")
        index = uint(child(links, "next"))
        tail = uint(child(links, "tail"))
        previous_kind, previous_index = "Entry", entry_index
        while True:
            if index >= len(extras) or index in visited:
                raise Unavailable("invalid/cyclic extra header link")
            visited.add(index)
            extra = extras[index]
            prev_kind, prev_value = enum(child(extra, "prev"))
            if (prev_kind != previous_kind or
                    uint(child(prev_value, "__0")) != previous_index):
                raise Unavailable("invalid previous header link")
            result.append((name, child(extra, "value")))
            next_kind, next_value = enum(child(extra, "next"))
            next_index = uint(child(next_value, "__0"))
            if next_kind == "Entry":
                if next_index != entry_index or tail != index:
                    raise Unavailable("invalid terminal header link")
                break
            if next_kind != "Extra":
                raise Unavailable("unsupported extra header link")
            previous_kind, previous_index = "Extra", index
            index = next_index
    if len(visited) != len(extras):
        raise Unavailable("unlinked extra header values")
    return result


class HeaderMapSynthetic:
    """Named header values, preserving duplicates and an expandable raw view."""

    def __init__(self, value, _dict):
        self.value = raw(value)
        self.children = []
        self.indices = {}
        self.update()

    def update(self):
        self.children = []
        # LLDB may resynthesize a cloned child despite SetPreferSyntheticValue.
        if self.value.GetName() == "[raw]":
            self.children = [self.value.GetChildAtIndex(index)
                             for index in range(self.value.GetNumChildren())]
            self.indices = {child.GetName(): index
                            for index, child in enumerate(self.children)}
            return False
        try:
            occurrences = {}
            for name, value in header_items(self.value):
                label = name.decode("ascii")
                occurrences[label] = occurrences.get(label, 0) + 1
                number = occurrences[label]
                if number > 1:
                    label += " [%d]" % number
                self.children.append(value.Clone(label))
        except Exception:
            # Keep every raw member available if decoding is not supported.
            self.children = [self.value.GetChildAtIndex(index)
                             for index in range(self.value.GetNumChildren())]
        raw = self.value.Clone("[raw]")
        raw.SetPreferSyntheticValue(False)
        self.children.append(raw)
        self.indices = {child.GetName(): index
                        for index, child in enumerate(self.children)}
        return False

    def num_children(self, _max=None):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index] if 0 <= index < len(self.children) else None

    def get_child_index(self, name):
        return self.indices.get(name, -1)

    def has_children(self):
        return bool(self.children)


@_summary
def incoming_body_summary(value):
    state, _ = enum(child(value, "state"))
    if state not in ("Unstarted", "Started", "Empty"):
        raise Unavailable("unsupported incoming body state")
    length_variant, length = enum(child(value, "content_length"))
    if length_variant == "None":
        size = "unknown content length"
    elif length_variant == "Some":
        size = "content length %d" % uint(child(length, "__0"))
    else:
        raise Unavailable("unsupported content length")
    return "streaming body (%s, %s)" % (state, size)


@_summary
def request_summary(value):
    head = child(value, "head")
    method = method_bytes(child(head, "method")).decode("ascii")
    uri = quote(uri_bytes(child(head, "uri")))
    names, count = header_counts(child(head, "headers"))
    summary = "%s %s | %d header values, %d names" % (method, uri, count, names)
    body = child(value, "body")
    if (body.GetTypeName() or "").startswith("wasip3::http_compat::IncomingBody<"):
        body_text = incoming_body_summary(body)
        summary += " | " + (body_text or "streaming body")
    return summary


# LLDB formatter registration


def _register_formatters(debugger):
    debugger.DeleteCategory(CATEGORY)
    category = debugger.CreateCategory(CATEGORY)
    summaries = {
        r"^alloc::string::String$": f"{__name__}.bytes_summary",
        r"^&(mut )?str$": f"{__name__}.bytes_summary",
        r"^bytes::bytes::Bytes$": f"{__name__}.bytes_summary",
        r"^http::byte_str::ByteStr$": f"{__name__}.bytes_summary",
        r"^alloc::vec::Vec<.*>$": f"{__name__}.vec_summary",
        r"^http::method::Method$": f"{__name__}.method_summary",
        r"^http::uri::scheme::Scheme$": f"{__name__}.scheme_summary",
        r"^http::uri::Uri$": f"{__name__}.uri_summary",
        r"^http::uri::authority::Authority$": f"{__name__}.authority_summary",
        r"^http::uri::path::PathAndQuery$": f"{__name__}.path_and_query_summary",
        r"^http::header::name::HeaderName$": f"{__name__}.header_name_summary",
        r"^http::header::value::HeaderValue$": f"{__name__}.header_value_summary",
        r"^http::header::map::HeaderMap<.*>$": f"{__name__}.header_map_summary",
        r"^http::request::Request<.*>$": f"{__name__}.request_summary",
        r"^wasip3::http_compat::IncomingBody<.*>$": f"{__name__}.incoming_body_summary",
    }
    for pattern, function in summaries.items():
        category.AddTypeSummary(lldb.SBTypeNameSpecifier(pattern, True),
                                lldb.SBTypeSummary.CreateWithFunctionName(function))
    for pattern, provider in {
        r"^http::header::map::HeaderMap<.*>$": f"{__name__}.HeaderMapSynthetic",
        r"^alloc::vec::Vec<.*>$": f"{__name__}.VecSynthetic",
        r"^alloc::string::String$": f"{__name__}.ByteBufferSynthetic",
        r"^&(mut )?str$": f"{__name__}.ByteBufferSynthetic",
        r"^bytes::bytes::Bytes$": f"{__name__}.ByteBufferSynthetic",
    }.items():
        category.AddTypeSynthetic(lldb.SBTypeNameSpecifier(pattern, True),
                                  lldb.SBTypeSynthetic.CreateWithClassName(provider))
    category.SetEnabled(True)
    _command(debugger, f"command script add --overwrite -f {__name__}.headers_command wasm-headers")


def _command(debugger, command):
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(command, result)
    if not result.Succeeded():
        raise RuntimeError(f"{command}: {result.GetError()}")
    return result.GetOutput() or ""


def __lldb_init_module(debugger, internal_dict):
    global lldb
    import lldb

    _command(debugger, f"command script add --overwrite -f {__name__}.target spin-debug-target")
    _command(debugger, f"command script add --overwrite -f {__name__}.start spin-debug-start")
    _command(debugger, f"command script add --overwrite -f {__name__}.stop spin-debug-stop")
    _register_formatters(debugger)
    atexit.register(_cleanup)


if __name__ == "__main__":
    raise SystemExit("Load this helper in LLDB: command script import /path/to/spin_debug.py")
