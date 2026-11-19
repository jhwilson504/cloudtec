#!/usr/bin/env python3
"""A stand-in for PuTTY's plink, for KasTec's SSH tests.

Hosts: "newhost.lab" has key SHA256:Zm9vYmFyYmF6cXV4cXV1eGNvcmdlZ3JhdWx0Z2FycGx5 (not cached unless -hostkey gives it);
any other host behaves the same with a key KasTec already trusts. Password "secret" signs in; then it's a tiny shell.
"""
import sys

KEY = "SHA256:Zm9vYmFyYmF6cXV4cXV1eGNvcmdlZ3JhdWx0Z2FycGx5"
args = sys.argv[1:]
opt = lambda name: args[args.index(name) + 1] if name in args else None  # noqa: E731
host, user, given = args[-1], opt("-l"), opt("-hostkey")
out = sys.stdout.buffer

if given is None:
    out.write(f"The host key is not cached for this server:\r\n  {host} (port {opt('-P')})\r\n"
              f"The server's ssh-ed25519 key fingerprint is:\r\n  ssh-ed25519 255 {KEY}\r\n".encode())
    sys.exit(1)
if given != KEY:
    out.write(b"WARNING - POTENTIAL SECURITY BREACH!\r\nThe host key does not match the one PuTTY has cached\r\n")
    sys.exit(1)
password = open(opt("-pwfile"), encoding="utf-8").read().strip()
if "-t" not in args or password != "secret":
    out.write(b"Access denied\r\nFATAL ERROR: Configured password was not accepted\r\n")
    sys.exit(1)

prompt = f"\x1b[1;32m{user}@{host}\x1b[0m:~$ ".encode()
out.write(b"Welcome to Ubuntu 22.04 LTS\r\n" + prompt)
out.flush()
line = b""
while True:
    ch = sys.stdin.buffer.read(1)
    if not ch:
        break
    if ch in (b"\r", b"\n"):
        cmd = line.decode()
        line = b""
        if cmd == "exit":
            out.write(b"\r\nlogout\r\n")
            out.flush()
            break
        out.write(f"\r\nyou typed: {cmd}\r\n".encode() + prompt)
    else:
        line += ch
        out.write(ch)   # the server's terminal echoes what you type
    out.flush()
