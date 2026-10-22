"""Create a private Windows directory while preserving restricted-token access.

No ACL on an existing path is broadened and no sandbox policy is changed.
"""
import ctypes as c
from ctypes import wintypes as w
import re


def private_mkdir(path):
    advapi = c.WinDLL("advapi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.POINTER(w.DWORD)]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), c.POINTER(w.DWORD)]
    advapi.GetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [c.c_void_p]

    class Attributes(c.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]
    class SidAttributes(c.Structure):
        _fields_ = [("sid", c.c_void_p), ("attributes", w.DWORD)]
    class Groups(c.Structure):
        _fields_ = [("count", w.DWORD), ("groups", SidAttributes * 1)]
    kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, c.POINTER(Attributes)]

    def check(ok):
        if not ok:
            raise OSError("private Windows directory unavailable", c.get_last_error())

    def sid_string(sid):
        value = w.LPWSTR()
        check(advapi.ConvertSidToStringSidW(sid, c.byref(value)))
        try:
            return value.value
        finally:
            kernel.LocalFree(c.cast(value, c.c_void_p))

    def descriptor_dacl(descriptor):
        encoded = w.LPWSTR()
        check(advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, c.byref(encoded), None))
        try:
            return encoded.value
        finally:
            kernel.LocalFree(c.cast(encoded, c.c_void_p))

    def directory_dacl(directory):
        size = w.DWORD()
        advapi.GetFileSecurityW(str(directory), 4, None, 0, c.byref(size))
        buffer = c.create_string_buffer(size.value)
        check(advapi.GetFileSecurityW(str(directory), 4, buffer, size, c.byref(size)))
        return descriptor_dacl(buffer)

    token = w.HANDLE()
    check(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, c.byref(token)))
    def token_info(kind):
        size = w.DWORD()
        advapi.GetTokenInformation(token, kind, None, 0, c.byref(size))
        buffer = c.create_string_buffer(size.value)
        check(advapi.GetTokenInformation(token, kind, buffer, size, c.byref(size)))
        return buffer
    try:
        user = token_info(1)  # TokenUser
        owner = sid_string(c.cast(user, c.POINTER(SidAttributes)).contents.sid)
        groups = token_info(11)  # TokenRestrictedSids
        count = c.cast(groups, c.POINTER(Groups)).contents.count
        entries = c.cast(c.addressof(groups) + Groups.groups.offset, c.POINTER(SidAttributes))
        restricted = {sid_string(entries[i].sid) for i in range(count)}
    finally:
        kernel.CloseHandle(token)
    # Only isolated principal SIDs may be retained. Generic groups such as
    # Everyone cannot serve as private-directory recipients.
    restricted = {sid for sid in restricted if sid.startswith(("S-1-5-21-", "S-1-15-2-"))}
    # MCP may start outside the shell's restricted token. Retain the narrow
    # named principals already authorized by the parent workspace so both can
    # use the store. Generic Everyone/Users/Authenticated Users grants are not
    # inherited. Existing denies remain an enforcement boundary on ancestors.
    parent_recipients = set()
    for ace in re.findall(r"\(([^()]*)\)", directory_dacl(path.parent)):
        parts = ace.split(";")
        if parts[0] == "A" and parts[-1].startswith(("S-1-5-21-", "S-1-15-2-")):
            parent_recipients.add(parts[-1])
    recipients = {owner, "SY", "BA"} | restricted | parent_recipients
    descriptor = c.c_void_p()
    sddl = "D:P" + "".join(f"(A;OICI;FA;;;{sid})" for sid in sorted(recipients))
    check(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None))
    try:
        # Windows can render a numeric account SID as an SDDL alias (for
        # example LA for the local Administrator used by hosted runners).
        # Canonicalize the intended and observed ACL through the same API;
        # accepting aliases by name alone would broaden the allowed set.
        canonical_recipients = {ace.split(";")[-1] for ace in
                                re.findall(r"\(([^()]*)\)", descriptor_dacl(descriptor))}
        attributes = Attributes(c.sizeof(Attributes), descriptor, False)
        created = kernel.CreateDirectoryW(str(path), c.byref(attributes))
        if not created and c.get_last_error() != 183:
            check(False)
    finally:
        kernel.LocalFree(descriptor)
    # Reused state must have the same narrow recipients. Never loosen an
    # existing directory or silently reuse a shared/writable artifact store.
    for ace in re.findall(r"\(([^()]*)\)", directory_dacl(path)):
        parts = ace.split(";")
        if parts[0] == "A" and parts[-1] not in canonical_recipients:
            raise OSError("existing Windows state directory is not private")
