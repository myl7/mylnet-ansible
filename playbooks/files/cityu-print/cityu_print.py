#!/usr/bin/env python3
"""cityu-print: a local web front end for an LPD print server behind a proxy.

macOS printing goes through CUPS backends that open raw TCP sockets and ignore
every proxy setting the system offers, so a print server that is only reachable
from another network cannot be added as a normal printer. This script replaces
that path. It speaks LPD (RFC 1179) itself and opens the connection through a
SOCKS5 proxy, an HTTP CONNECT proxy, or an SSH host, then serves a small upload
page on localhost.

Transports:
    direct                      plain TCP from this machine
    socks5://host:port          SOCKS5, optional user:pass@
    http://host:port            HTTP CONNECT, optional user:pass@
    ssh://[user@]host[:port]    runs `ssh -W target:port host`, no port forward

Examples:
    ./cityu_print.py --transport ssh://cu
    ./cityu_print.py --transport socks5://127.0.0.1:1080
    ./cityu_print.py --transport ssh://cu --check
    ./cityu_print.py --transport ssh://cu --print report.pdf --queue g7352
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import random
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_SERVER = "144.214.36.203"
DEFAULT_PORT = 515
# Only the queue actually in use. The page accepts any name typed into the
# field and checks it against the server, so the list is a convenience.
DEFAULT_QUEUES = ["g7352"]
MAX_UPLOAD = 256 * 1024 * 1024
UEL = b"\x1b%-12345X"


# --------------------------------------------------------------------------
# streams
# --------------------------------------------------------------------------

class Stream:
    """Minimal bidirectional byte stream, backed by a socket or a subprocess."""

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def recv(self, size: int) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SocketStream(Stream):
    def __init__(self, sock: socket.socket):
        self.sock = sock

    def send(self, data):
        self.sock.sendall(data)

    def recv(self, size):
        return self.sock.recv(size)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class ProcessStream(Stream):
    """Wraps `ssh -W`, whose stdin and stdout are the remote connection."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc

    def send(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def recv(self, size):
        return self.proc.stdout.read1(size)

    def close(self):
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _read_exact(sock: socket.socket, size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("proxy closed the connection early")
        buf += chunk
    return buf


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------

class Transport:
    """Opens a Stream to an arbitrary host and port."""

    def __init__(self, spec: str, timeout: float = 20.0):
        self.spec = spec
        self.timeout = timeout
        self.url = urlparse(spec if "://" in spec else spec + "://")
        self.scheme = self.url.scheme or "direct"
        if self.scheme not in ("direct", "socks5", "socks5h", "http", "ssh"):
            raise ValueError(f"unsupported transport: {spec}")

    def describe(self) -> str:
        if self.scheme == "direct":
            return "direct TCP"
        return self.spec

    def open(self, host: str, port: int) -> Stream:
        if self.scheme == "direct":
            return SocketStream(socket.create_connection((host, port), self.timeout))
        if self.scheme in ("socks5", "socks5h"):
            return SocketStream(self._socks5(host, port))
        if self.scheme == "http":
            return SocketStream(self._http_connect(host, port))
        return self._ssh(host, port)

    # -- SOCKS5 ------------------------------------------------------------

    def _socks5(self, host: str, port: int) -> socket.socket:
        sock = socket.create_connection(
            (self.url.hostname, self.url.port or 1080), self.timeout
        )
        sock.settimeout(self.timeout)
        user, password = self.url.username, self.url.password
        sock.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
        ver, method = _read_exact(sock, 2)
        if ver != 5:
            raise ConnectionError("proxy is not SOCKS5")
        if method == 0x02:
            if not user:
                raise ConnectionError("proxy wants a username and password")
            u, p = user.encode(), (password or "").encode()
            sock.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            if _read_exact(sock, 2)[1] != 0:
                raise ConnectionError("proxy rejected the credentials")
        elif method != 0x00:
            raise ConnectionError("proxy offered no acceptable auth method")

        try:
            addr = b"\x01" + socket.inet_aton(host)
        except OSError:
            raw = host.encode()
            addr = b"\x03" + bytes([len(raw)]) + raw
        sock.sendall(b"\x05\x01\x00" + addr + port.to_bytes(2, "big"))
        reply = _read_exact(sock, 4)
        if reply[1] != 0:
            errors = {
                1: "general failure", 2: "connection not allowed", 3: "network unreachable",
                4: "host unreachable", 5: "connection refused", 6: "TTL expired",
                7: "command not supported", 8: "address type not supported",
            }
            raise ConnectionError(
                f"SOCKS5 refused {host}:{port}: {errors.get(reply[1], reply[1])}"
            )
        atyp = reply[3]
        if atyp == 1:
            _read_exact(sock, 4 + 2)
        elif atyp == 3:
            _read_exact(sock, _read_exact(sock, 1)[0] + 2)
        elif atyp == 4:
            _read_exact(sock, 16 + 2)
        return sock

    # -- HTTP CONNECT ------------------------------------------------------

    def _http_connect(self, host: str, port: int) -> socket.socket:
        sock = socket.create_connection(
            (self.url.hostname, self.url.port or 8080), self.timeout
        )
        sock.settimeout(self.timeout)
        target = f"{host}:{port}"
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
        if self.url.username:
            token = f"{self.url.username}:{self.url.password or ''}".encode()
            lines.append(
                "Proxy-Authorization: Basic " + base64.b64encode(token).decode()
            )
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        header = b""
        while b"\r\n\r\n" not in header:
            byte = sock.recv(1)
            if not byte:
                raise ConnectionError("proxy closed the connection during CONNECT")
            header += byte
            if len(header) > 8192:
                raise ConnectionError("proxy sent an oversized CONNECT response")
        status = header.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 200 " not in status:
            raise ConnectionError(f"proxy refused CONNECT {target}: {status}")
        return sock

    # -- SSH ---------------------------------------------------------------

    def _ssh(self, host: str, port: int) -> Stream:
        target = self.url.hostname
        if not target:
            raise ValueError("ssh transport needs a host, e.g. ssh://cu")
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(self.timeout)}"]
        # Reuse one SSH connection across jobs so only the first one pays setup cost.
        cmd += [
            "-o", "ControlMaster=auto",
            "-o", "ControlPath=~/.ssh/cityu-print-%r@%h:%p",
            "-o", "ControlPersist=600",
        ]
        if self.url.port:
            cmd += ["-p", str(self.url.port)]
        if self.url.username:
            cmd += ["-l", self.url.username]
        cmd += ["-W", f"{host}:{port}", target]
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return ProcessStream(proc)


# --------------------------------------------------------------------------
# LPD client (RFC 1179)
# --------------------------------------------------------------------------

class LPDError(Exception):
    pass


def _expect_ack(stream: Stream, what: str) -> None:
    reply = stream.recv(1)
    if reply == b"":
        raise LPDError(f"server closed the connection after {what}")
    if reply != b"\x00":
        raise LPDError(f"server rejected {what} (code {reply[0]})")


def _send_subfile(stream: Stream, code: int, name: str, payload: bytes) -> None:
    stream.send(f"{chr(code)}{len(payload)} {name}\n".encode())
    _expect_ack(stream, f"the {name} header")
    stream.send(payload)
    stream.send(b"\x00")
    _expect_ack(stream, f"the {name} body")


def lpd_send(
    transport: Transport,
    server: str,
    port: int,
    queue: str,
    payload: bytes,
    *,
    user: str,
    origin: str,
    job_name: str,
    doc_name: str,
) -> str:
    """Submit one job. Returns the LPD job id."""
    job_id = f"{random.randint(1, 999):03d}"
    cf_name = f"cfA{job_id}{origin}"
    df_name = f"dfA{job_id}{origin}"
    control = "".join(
        line + "\n"
        for line in [
            f"H{origin}",
            f"P{user}",
            f"J{job_name}",
            f"N{doc_name}",
            f"l{df_name}",   # print the data file verbatim
            f"U{df_name}",   # and remove it afterwards
        ]
    ).encode()

    with transport.open(server, port) as stream:
        stream.send(f"\x02{queue}\n".encode())
        _expect_ack(stream, f"the job for queue {queue}")
        _send_subfile(stream, 0x02, cf_name, control)
        _send_subfile(stream, 0x03, df_name, payload)
    return job_id


def lpd_queue_exists(transport: Transport, server: str, port: int, queue: str) -> bool:
    """Ask the server to receive a job, then hang up without sending one.

    A Windows LPD server acknowledges a known queue with 0x00 and refuses an
    unknown one, which makes this a credential free way to check a name. The
    job that is never sent is discarded by the server."""
    with transport.open(server, port) as stream:
        stream.send(f"\x02{queue}\n".encode())
        return stream.recv(1) == b"\x00"


def lpd_queue_state(
    transport: Transport, server: str, port: int, queue: str, long_form: bool = True
) -> str:
    with transport.open(server, port) as stream:
        stream.send(bytes([0x04 if long_form else 0x03]) + f"{queue}\n".encode())
        chunks = []
        while True:
            chunk = stream.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > 1 << 20:
                break
    return b"".join(chunks).decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# document preparation
# --------------------------------------------------------------------------

def detect_language(data: bytes) -> str:
    if data.startswith(UEL):
        return "PJL"
    if data.startswith(b"%PDF"):
        return "PDF"
    if data.startswith(b"%!"):
        return "POSTSCRIPT"
    if data.startswith(b"\x1b"):
        return "PCL"
    return "TEXT"


def pdf_to_postscript(data: bytes) -> bytes:
    """Convert with Ghostscript. Windows print servers pass raw bytes straight
    to the printer, and PostScript is the format every model here understands."""
    gs = shutil.which("gs")
    if not gs:
        raise RuntimeError("ghostscript is not installed, use --format raw")
    result = subprocess.run(
        [gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=ps2write",
         "-sOutputFile=-", "-"],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode(errors="replace").strip()[:400]
        raise RuntimeError(f"ghostscript failed: {detail}")
    return result.stdout


def wrap_pjl(data: bytes, *, copies: int, duplex: str, job_name: str) -> bytes:
    """Add a PJL header for copies and duplex. Skipped when the job already
    carries one, so we never nest two PJL envelopes."""
    if detect_language(data) == "PJL":
        return data
    if copies <= 1 and duplex == "default":
        return data
    language = detect_language(data)
    if language not in ("POSTSCRIPT", "PCL", "PDF"):
        return data

    safe_name = "".join(c for c in job_name if c in string.printable and c != '"')[:64]
    header = [UEL + b"@PJL\n", f'@PJL JOB NAME="{safe_name}"\n'.encode()]
    if copies > 1:
        header.append(f"@PJL SET COPIES={copies}\n".encode())
    if duplex == "off":
        header.append(b"@PJL SET DUPLEX=OFF\n")
    elif duplex in ("long", "short"):
        header.append(b"@PJL SET DUPLEX=ON\n")
        edge = b"LONGEDGE" if duplex == "long" else b"SHORTEDGE"
        header.append(b"@PJL SET BINDING=" + edge + b"\n")
    header.append(f"@PJL ENTER LANGUAGE={language}\n".encode())
    return b"".join(header) + data + UEL + b"@PJL EOJ\n" + UEL


def prepare(data: bytes, *, fmt: str, copies: int, duplex: str, job_name: str) -> tuple[bytes, str]:
    language = detect_language(data)
    note = language.lower()
    if fmt == "ps" or (fmt == "auto" and language == "PDF" and shutil.which("gs")):
        if language == "PDF":
            data = pdf_to_postscript(data)
            note = "pdf converted to postscript"
        elif language not in ("POSTSCRIPT", "PJL"):
            raise RuntimeError(f"cannot convert {language} to postscript")
    return wrap_pjl(data, copies=copies, duplex=duplex, job_name=job_name), note


# --------------------------------------------------------------------------
# web server
# --------------------------------------------------------------------------

PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>cityu-print</title>
<style>
:root{color-scheme:light dark;--bg:#fbfbfa;--fg:#1a1a19;--mut:#6b6b68;--line:#e0e0dc;--card:#fff;--acc:#2f6f4e}
@media (prefers-color-scheme:dark){:root{--bg:#17181a;--fg:#e8e8e6;--mut:#9a9a96;--line:#2c2e31;--card:#1f2023;--acc:#7fc0a0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:19px;margin:0 0 4px}
.sub{color:var(--mut);font-size:13px;margin:0 0 24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin-bottom:16px}
#drop{border:1.5px dashed var(--line);border-radius:10px;padding:34px 18px;text-align:center;color:var(--mut);cursor:pointer;transition:.15s}
#drop.hot{border-color:var(--acc);color:var(--fg)}
#files{margin-top:12px;font-size:13px}
#files div{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--line)}
.row{display:flex;gap:14px;flex-wrap:wrap;margin-top:14px}
.controls{display:grid;grid-template-columns:1.5fr .7fr 1.3fr 1fr;gap:14px;margin-top:16px}
label{display:block;font-size:12px;color:var(--mut);margin-bottom:4px}
select,input{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:7px 9px;font:inherit}
button{background:var(--acc);color:#fff;border:0;border-radius:7px;padding:9px 18px;font:inherit;font-weight:600;cursor:pointer}
button.ghost{background:transparent;color:var(--fg);border:1px solid var(--line);font-weight:400}
button:disabled{opacity:.5;cursor:default}
#qstat{font-weight:600}
@media (max-width:640px){
  .wrap{padding:20px 14px 48px}
  .controls{grid-template-columns:1fr 1fr}
  /* 16px keeps iOS from zooming in when a field takes focus. */
  select,input{font-size:16px}
  .row{gap:10px}
  .row button{flex:1 1 auto}
}
#duplexbox{margin-top:18px;border-top:1px solid var(--line);padding-top:6px}
#duplexart{display:block;width:100%;max-width:340px;margin:0 auto}
#duplexnote{margin:2px 0 0;text-align:center;color:var(--mut);font-size:12px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:12px;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-word;max-height:340px;overflow:auto;margin:0}
.ok{color:var(--acc)}.bad{color:#c0392b}
@media (prefers-color-scheme:dark){.bad{color:#e8827a}}
</style>
<div class="wrap">
<h1>cityu-print</h1>
<p class="sub">__SUB__</p>

<div class="card">
  <div id="drop">Drop files here, or click to choose<br><span style="font-size:12px">PDF, PostScript, PCL or plain text</span></div>
  <input id="picker" type="file" multiple hidden>
  <div id="files"></div>
  <div class="controls">
    <div><label>Queue <span id="qstat"></span></label>
      <input id="queue" list="queues" value="__DEFAULT_QUEUE__" autocomplete="off">
      <datalist id="queues">__QUEUES__</datalist></div>
    <div><label>Copies</label><input id="copies" type="number" value="1" min="1" max="99"></div>
    <div><label>Duplex</label><select id="duplex">
      <option value="default">Printer default</option>
      <option value="off">One sided</option>
      <option value="long">Two sided, long edge</option>
      <option value="short">Two sided, short edge</option>
    </select></div>
    <div><label>Format</label><select id="format">
      <option value="auto">Auto</option>
      <option value="ps">Force PostScript</option>
      <option value="raw">Send as is</option>
    </select></div>
  </div>
  <div id="duplexbox">
    <svg id="duplexart" viewBox="0 0 340 150" role="img" aria-label="duplex illustration"></svg>
    <p id="duplexnote"></p>
  </div>
  <div class="row" style="margin-top:18px">
    <button id="send" disabled>Print</button>
    <button id="check" class="ghost">Show queue</button>
    <button id="ping" class="ghost">Test connection</button>
  </div>
</div>

<div class="card"><pre id="log">ready</pre></div>
</div>
<script>
const picker=document.getElementById('picker'),drop=document.getElementById('drop');
const list=document.getElementById('files'),log=document.getElementById('log'),send=document.getElementById('send');
let files=[];
function say(msg,cls){const t=new Date().toTimeString().slice(0,8);log.innerHTML+=`\\n<span class="${cls||''}">[${t}] ${msg}</span>`;log.scrollTop=log.scrollHeight;}
function render(){list.innerHTML=files.map((f,i)=>`<div><span>${f.name}</span><span>${(f.size/1024).toFixed(0)} KB</span></div>`).join('');send.disabled=!files.length;}
if(matchMedia('(hover:none)').matches)
  drop.firstChild.textContent='Tap to choose files';
drop.onclick=()=>picker.click();
picker.onchange=e=>{files=[...e.target.files];render();};
drop.ondragover=e=>{e.preventDefault();drop.classList.add('hot');};
drop.ondragleave=()=>drop.classList.remove('hot');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('hot');files=[...e.dataTransfer.files];render();};
send.onclick=async()=>{
  send.disabled=true;
  const q=new URLSearchParams({queue:queue.value,copies:copies.value,duplex:duplex.value,format:format.value});
  for(const f of files){
    q.set('name',f.name);
    say(`sending ${f.name} ...`);
    try{
      const r=await fetch('/api/print?'+q,{method:'POST',body:f,headers:{'X-CityU-Print':'1'}});
      const j=await r.json();
      j.ok?say(`${f.name}: job ${j.job_id} accepted by ${j.queue} (${j.note}, ${(j.bytes/1024).toFixed(0)} KB)`,'ok')
          :say(`${f.name}: ${j.error}`,'bad');
    }catch(err){say(`${f.name}: ${err}`,'bad');}
  }
  send.disabled=false;
};
function sheet(x,o){
  let g=`<g transform="translate(${x},14)">`;
  g+=`<rect width="70" height="92" rx="3" fill="var(--card)" stroke="var(--line)"/>`;
  if(o.page){
    g+=`<g${o.rot?' transform="rotate(180 35 46)"':''}>`;
    for(let i=0;i<5;i++)g+=`<rect x="12" y="${22+i*10}" width="${i===4?26:46}" height="3.5" rx="1.75" fill="var(--mut)" opacity=".5"/>`;
    g+=`<text x="58" y="84" font-size="13" font-weight="700" fill="var(--acc)" text-anchor="end">${o.page}</text></g>`;
  }else{
    g+=`<text x="35" y="50" font-size="10" fill="var(--mut)" text-anchor="middle">blank</text>`;
  }
  const bars={left:[-2,0,4,92],right:[68,0,4,92],top:[0,-2,70,4],bottom:[0,90,70,4]};
  if(bars[o.bind]){const b=bars[o.bind];
    g+=`<rect x="${b[0]}" y="${b[1]}" width="${b[2]}" height="${b[3]}" rx="2" fill="var(--acc)"/>`;}
  g+=`<text x="35" y="116" font-size="10" fill="var(--mut)" text-anchor="middle">${o.label}</text></g>`;
  return g;
}
const ARROW='stroke="var(--mut)" fill="none" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"';
// The back is drawn as it looks once the sheet has been turned the way the
// binding intends, so the bound edge moves and the text stays upright.
const flipH=`<path d="M158 60 h24 M164 55 l-6 5 l6 5 M176 55 l6 5 l-6 5" ${ARROW}/>`;
const flipV=`<path d="M170 48 v24 M165 54 l5 -6 l5 6 M165 66 l5 6 l5 -6" ${ARROW}/>`;
const DUPLEX={
  'default':{art:()=>sheet(135,{page:'1',label:'front'}),
    note:'The queue keeps its own setting. Pick one of the others to be sure.'},
  'off':{art:()=>sheet(65,{page:'1',label:'sheet 1, front'})+sheet(205,{page:'2',label:'sheet 2, front'}),
    note:'Every page gets its own sheet, printed on the front. The backs stay blank.'},
  'long':{art:()=>sheet(65,{page:'1',label:'front',bind:'left'})+flipH+sheet(205,{page:'2',label:'back',bind:'right'}),
    note:'Bound along the long edge. Turn the sheet left to right, like a book, and the bound edge ends up on the right.'},
  'short':{art:()=>sheet(65,{page:'1',label:'front',bind:'top'})+flipV+sheet(205,{page:'2',label:'back',bind:'bottom'}),
    note:'Bound along the short edge. Turn the sheet top to bottom, like a notepad, and the bound edge ends up at the foot.'}
};
function drawDuplex(){
  const d=DUPLEX[duplex.value];
  document.getElementById('duplexart').innerHTML=d.art();
  document.getElementById('duplexnote').textContent=d.note;
}
duplex.onchange=drawDuplex;drawDuplex();
let qtimer;
async function checkQueue(){
  const name=queue.value.trim();
  const stat=document.getElementById('qstat');
  if(!name){stat.textContent='';return;}
  stat.textContent='...';stat.className='';
  try{
    const r=await fetch('/api/validate?queue='+encodeURIComponent(name));
    const j=await r.json();
    stat.textContent=j.ok?(j.exists?'exists':'unknown'):'unreachable';
    stat.className=j.ok&&j.exists?'ok':'bad';
  }catch(e){stat.textContent='unreachable';stat.className='bad';}
}
queue.oninput=()=>{clearTimeout(qtimer);qtimer=setTimeout(checkQueue,500);};
checkQueue();
check.onclick=async()=>{
  say(`queue ${queue.value}:`);
  const r=await fetch('/api/queue?queue='+encodeURIComponent(queue.value));
  const j=await r.json();
  say(j.ok?(j.state.trim()||'(empty)'):j.error,j.ok?'':'bad');
};
ping.onclick=async()=>{
  say('testing transport ...');
  const r=await fetch('/api/check');const j=await r.json();
  say(j.ok?`reachable via ${j.transport}, ${j.ms} ms`:j.error,j.ok?'ok':'bad');
};
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "cityu-print/1.0"
    config: dict = {}

    def log_message(self, fmt, *args):
        sys.stderr.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        cfg = self.config
        if url.path == "/":
            options = "".join(f"<option>{q}</option>" for q in cfg["queues"])
            page = PAGE.replace("__DEFAULT_QUEUE__", cfg["queues"][0])
            sub = f"{cfg['server']}:{cfg['port']} via {cfg['transport'].describe()}"
            body = page.replace("__QUEUES__", options).replace("__SUB__", sub).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if url.path == "/api/check":
            start = time.time()
            try:
                with cfg["transport"].open(cfg["server"], cfg["port"]):
                    pass
                self._json({
                    "ok": True,
                    "transport": cfg["transport"].describe(),
                    "ms": int((time.time() - start) * 1000),
                })
            except Exception as exc:
                self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return

        if url.path == "/api/validate":
            queue = parse_qs(url.query).get("queue", [""])[0]
            try:
                self._json({"ok": True, "exists": lpd_queue_exists(
                    cfg["transport"], cfg["server"], cfg["port"], queue)})
            except Exception as exc:
                self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return

        if url.path == "/api/queue":
            queue = parse_qs(url.query).get("queue", [cfg["queues"][0]])[0]
            try:
                state = lpd_queue_state(cfg["transport"], cfg["server"], cfg["port"], queue)
                self._json({"ok": True, "state": state})
            except Exception as exc:
                self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return

        self.send_error(404)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/print":
            self.send_error(404)
            return

        if self.headers.get("X-CityU-Print") != "1":
            self._json({"ok": False, "error": "missing X-CityU-Print header"}, 403)
            return

        cfg = self.config
        args = parse_qs(url.query)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD:
            self._json({"ok": False, "error": "empty or oversized upload"}, 400)
            return

        data = self.rfile.read(length)
        name = args.get("name", ["document"])[0]
        queue = args.get("queue", [cfg["queues"][0]])[0]
        copies = max(1, min(99, int(args.get("copies", ["1"])[0] or 1)))
        duplex = args.get("duplex", ["default"])[0]
        fmt = args.get("format", ["auto"])[0]

        try:
            payload, note = prepare(
                data, fmt=fmt, copies=copies, duplex=duplex, job_name=name
            )
            job_id = lpd_send(
                cfg["transport"], cfg["server"], cfg["port"], queue, payload,
                user=cfg["user"], origin=cfg["origin"], job_name=name, doc_name=name,
            )
            self._json({
                "ok": True, "job_id": job_id, "queue": queue,
                "bytes": len(payload), "note": note,
            })
        except Exception as exc:
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", default=os.environ.get("CITYU_PRINT_TRANSPORT", "direct"),
                        help="direct, socks5://host:port, http://host:port or ssh://host")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="LPD server address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--queue", default=DEFAULT_QUEUES[0], help="default queue")
    parser.add_argument("--queues", default=",".join(DEFAULT_QUEUES),
                        help="comma separated queue list shown in the page")
    parser.add_argument("--user", default=getpass.getuser(), help="name recorded on the job")
    parser.add_argument("--origin", default=socket.gethostname().split(".")[0],
                        help="host name recorded on the job")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8631)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--open", action="store_true", help="open the page in a browser")
    parser.add_argument("--check", action="store_true", help="test the transport and exit")
    parser.add_argument("--state", action="store_true", help="print the queue state and exit")
    parser.add_argument("--print", dest="print_file", help="submit one file and exit")
    parser.add_argument("--copies", type=int, default=1)
    parser.add_argument("--duplex", default="default", choices=["default", "off", "long", "short"])
    parser.add_argument("--format", default="auto", choices=["auto", "ps", "raw"])
    opts = parser.parse_args()

    transport = Transport(opts.transport, timeout=opts.timeout)
    queues = [q.strip() for q in opts.queues.split(",") if q.strip()]
    if opts.queue not in queues:
        queues.insert(0, opts.queue)

    if opts.check:
        start = time.time()
        try:
            with transport.open(opts.server, opts.port):
                pass
        except Exception as exc:
            print(f"unreachable: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"{opts.server}:{opts.port} reachable via {transport.describe()} "
              f"in {int((time.time() - start) * 1000)} ms")
        return 0

    if opts.state:
        print(lpd_queue_state(transport, opts.server, opts.port, opts.queue))
        return 0

    if opts.print_file:
        with open(opts.print_file, "rb") as handle:
            data = handle.read()
        name = os.path.basename(opts.print_file)
        payload, note = prepare(data, fmt=opts.format, copies=opts.copies,
                                duplex=opts.duplex, job_name=name)
        job_id = lpd_send(transport, opts.server, opts.port, opts.queue, payload,
                          user=opts.user, origin=opts.origin, job_name=name, doc_name=name)
        print(f"job {job_id} accepted by {opts.queue} ({note}, {len(payload)} bytes)")
        return 0

    Handler.config = {
        "transport": transport, "server": opts.server, "port": opts.port,
        "queues": queues, "user": opts.user, "origin": opts.origin,
    }
    httpd = ThreadingHTTPServer((opts.bind, opts.web_port), Handler)
    url = f"http://{opts.bind}:{opts.web_port}/"
    print(f"cityu-print on {url}")
    print(f"target {opts.server}:{opts.port} via {transport.describe()}")
    if opts.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
