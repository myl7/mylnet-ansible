#!/usr/bin/env python3
"""List GPU processes and request kills through the gpu-idle-kill service.

  gpu-idle-kill list         every process holding a GPU, with its idle clock
  gpu-idle-kill kill <pid>   end one process, if it has been idle long enough

The service, not this client, decides eligibility. See docs/gpu-idle-kill.md.
"""

import argparse
import socket
import sys

DEFAULT_SOCKET = "/run/gpu-idle-kill/kill.sock"


def talk(path: str, request: str) -> str:
    """Send one request and read the response to EOF.

    Exits with status 2 when the service is unreachable, so callers can tell
    a refused request from a dead service.
    """
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(10.0)
            sock.connect(path)
            sock.sendall((request + "\n").encode())
            chunks = []
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
    except OSError as err:
        print(f"cannot reach the gpu-idle-kill service at {path}: {err}", file=sys.stderr)
        raise SystemExit(2)
    return b"".join(chunks).decode(errors="replace")


def human_idle(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


HEADERS = ("PID", "USER", "GPU", "STATE", "IDLE", "COMMAND")


def render_list(response: str) -> str:
    """Render a LIST response: a header line, then one row per process.

    A row is pid, user, gpus, status, idle seconds, then the command line as
    the free-form tail.
    """
    header, *rows = response.splitlines()
    fields = dict(part.split("=", 1) for part in header.split()[2:])
    dry_run = fields["dry_run"] == "1"
    entries = []
    for row in rows:
        parts = row.split(maxsplit=5)
        pid, user, gpus, status, idle = parts[:5]
        cmd = parts[5] if len(parts) > 5 else ""
        state = {"idle": "", "eligible": "ELIGIBLE", "ending": "ENDING"}[status]
        entries.append((pid, user, gpus, state or "idle", human_idle(float(idle)), cmd))
    if not entries:
        return "no process holds a GPU context right now"
    widths = [max(len(str(row[i])) for row in entries + [HEADERS]) for i in range(len(HEADERS))]
    lines = ["  ".join(str(v).ljust(widths[i]) for i, v in enumerate(HEADERS)).rstrip()]
    for entry in entries:
        lines.append("  ".join(str(v).ljust(widths[i]) for i, v in enumerate(entry)).rstrip())
    if dry_run:
        lines.append("dry-run: requests are checked and logged, but nothing is signalled yet")
    lines.append("run gpu-idle-kill kill <pid> to end an ELIGIBLE process")
    return "\n".join(lines)


def do_list(path: str) -> int:
    response = talk(path, "LIST").strip()
    if not response.startswith("OK"):
        print(response.removeprefix("NO "), file=sys.stderr)
        return 1
    print(render_list(response))
    return 0


def do_kill(path: str, pid: int) -> int:
    response = talk(path, f"KILL {pid}").strip()
    status, _, message = response.partition(" ")
    print(message or status)
    return 0 if status == "OK" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gpu-idle-kill", description=__doc__.splitlines()[0])
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help="path of the service socket")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show every process holding a GPU")
    kill_parser = sub.add_parser("kill", help="end one process, if it has been idle long enough")
    kill_parser.add_argument("pid", type=int)
    args = parser.parse_args(argv)
    if args.command == "list":
        return do_list(args.socket)
    return do_kill(args.socket, args.pid)


if __name__ == "__main__":
    sys.exit(main())
