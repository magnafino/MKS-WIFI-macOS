#!/usr/bin/env python3
"""
mks_send - Send a .gco/.gcode file to an MKS Robin WiFi / MKS TFT WiFi
board over the local network, without needing Cura installed.

Protocol (reverse-engineered from the official "MKS WiFi Plugin" for Cura,
https://github.com/PrintMakerLab/mks-wifi-plugin):

  1. Upload:  HTTP POST http://<printer-ip>/upload?X-Filename=<name>
              Content-Type: application/octet-stream
              body = raw file bytes

  2. Start print (optional): open a raw TCP connection to
              <printer-ip>:8080 and send:
                  M23 <filename>\\r\\n
                  M24\\r\\n

Usage:
    mks_send.py <printer-ip> [options] <path-to-gcode-file>

(printer_ip comes first and gcode_file comes last on purpose: slicers
such as PrusaSlicer call post-processing scripts as
    <your configured command line> <full-path-to-sliced-gcode>
i.e. they always append the gcode file path as the final argument, so
that argument position is reserved for it.)

Examples:
    mks_send.py 192.168.1.50 ~/Desktop/part.gco
    mks_send.py 192.168.1.50 --start-print ~/Desktop/part.gco
    mks_send.py 192.168.1.50:8080 --start-print ~/Desktop/part.gco

By default a small progress window (KB sent, percentage, Cancel button)
is shown. Pass --no-gui to use a console progress bar instead (useful
if tkinter isn't available or you're running this from a terminal).

PrusaSlicer "Post-processing scripts" field (Windows example):
    "C:\\Python311\\python.exe" "C:\\Tools\\mks_send.py" 192.168.0.33 --start-print
(PrusaSlicer appends the sliced .gcode path automatically — don't add it yourself.)
"""

import argparse
import http.client
import os
import queue
import socket
import sys
import threading
import time
import urllib.parse

DEFAULT_HTTP_PORT = 80
DEFAULT_PRINTER_PORT = 8080
CHUNK_SIZE = 32 * 1024  # 32 KiB per progress update

try:
    import tkinter as tk
    HAS_TK = True
except ImportError:
    HAS_TK = False


class UploadCancelled(Exception):
    """Raised internally when the user cancels the upload."""


def parse_address(address: str, default_port: int):
    """Split 'host' or 'host:port' into (host, port)."""
    if ":" in address:
        host, port_str = address.rsplit(":", 1)
        try:
            return host, int(port_str)
        except ValueError:
            return address, default_port
    return address, default_port


def check_binary_gcode(data: bytes) -> None:
    if data[:4] == b"GCDE":
        print(
            "Warning: this looks like PrusaSlicer's binary G-code format (.bgcode), "
            "which MKS Robin/TFT boards can't read. In PrusaSlicer, disable "
            "'Use binary G-code' under Print Settings -> Output options, or export "
            "plain-text G-code.",
            file=sys.stderr,
        )


def _do_upload(host, http_port, data, remote_name, timeout, progress_cb, cancel_check):
    """
    Shared upload implementation.
    progress_cb(sent, total) is called after each chunk.
    cancel_check() should return True if the upload should be aborted;
    raises UploadCancelled if so.
    """
    total = len(data)
    query = urllib.parse.urlencode({"X-Filename": remote_name})
    url_path = f"/upload?{query}"

    conn = http.client.HTTPConnection(host, http_port, timeout=timeout)
    try:
        conn.putrequest("POST", url_path)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(total))
        conn.putheader("Connection", "keep-alive")
        conn.endheaders()

        sent = 0
        progress_cb(sent, total)
        for offset in range(0, total, CHUNK_SIZE):
            if cancel_check():
                raise UploadCancelled()
            chunk = data[offset:offset + CHUNK_SIZE]
            conn.send(chunk)
            sent += len(chunk)
            progress_cb(sent, total)

        if cancel_check():
            raise UploadCancelled()

        response = conn.getresponse()
        body = response.read()
        if response.status >= 400:
            raise RuntimeError(
                f"Printer returned HTTP {response.status} {response.reason}: "
                f"{body[:200]!r}"
            )
        return response.status, response.reason
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Console mode (no GUI)
# ---------------------------------------------------------------------------

def upload_file_console(host, http_port, filepath, remote_name, timeout):
    with open(filepath, "rb") as f:
        data = f.read()
    check_binary_gcode(data)

    def progress_cb(sent, total):
        pct = (sent / total * 100) if total else 100
        bar_len = 30
        filled = int(bar_len * sent / total) if total else bar_len
        bar = "#" * filled + "-" * (bar_len - filled)
        sys.stdout.write(
            f"\rUploading [{bar}] {pct:5.1f}%  ({sent // 1024} KB / {total // 1024} KB)"
        )
        sys.stdout.flush()

    status, reason = _do_upload(host, http_port, data, remote_name, timeout, progress_cb, lambda: False)
    print()
    print(f"Upload finished: HTTP {status} {reason}")


# ---------------------------------------------------------------------------
# GUI mode
# ---------------------------------------------------------------------------

def upload_file_gui(host, http_port, filepath, remote_name, timeout):
    """
    Shows a small progress window (KB sent, percentage, Cancel button)
    while uploading in a background thread. Raises UploadCancelled if the
    user cancels, or re-raises whatever error the upload hit.
    """
    with open(filepath, "rb") as f:
        data = f.read()
    check_binary_gcode(data)
    total = len(data)

    result_queue = queue.Queue()
    cancel_event = threading.Event()

    def worker():
        try:
            status, reason = _do_upload(
                host, http_port, data, remote_name, timeout,
                progress_cb=lambda sent, tot: result_queue.put(("progress", sent, tot)),
                cancel_check=cancel_event.is_set,
            )
            result_queue.put(("done", status, reason))
        except UploadCancelled:
            result_queue.put(("cancelled",))
        except Exception as e:  # noqa: BLE001 - surface any error to the GUI thread
            result_queue.put(("error", e))

    root = tk.Tk()
    root.title("MKS WiFi Upload")
    BG = "#f2f2f2"
    FG = "#000000"
    root.configure(bg=BG)
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    padding = {"padx": 16, "pady": 8}

    tk.Label(root, text=f"Sending {os.path.basename(filepath)} to {host}",
             bg=BG, fg=FG).pack(**padding)

    BAR_WIDTH, BAR_HEIGHT = 320, 20
    bar_canvas = tk.Canvas(root, width=BAR_WIDTH, height=BAR_HEIGHT,
                            bg="#e0e0e0", highlightthickness=1, highlightbackground="#a0a0a0")
    bar_canvas.pack(padx=16, pady=(0, 4))
    bar_fill = bar_canvas.create_rectangle(0, 0, 0, BAR_HEIGHT, fill="#3a7cf0", width=0)

    def set_progress(sent, tot):
        frac = (sent / tot) if tot else 1.0
        bar_canvas.coords(bar_fill, 0, 0, BAR_WIDTH * frac, BAR_HEIGHT)

    status_var = tk.StringVar(value=f"0 KB / {total // 1024} KB (0%)")
    tk.Label(root, textvariable=status_var, bg=BG, fg=FG).pack(pady=(0, 8))

    outcome = {"value": None}

    def on_cancel():
        cancel_event.set()
        cancel_button.config(state="disabled", text="Cancelling...")
        status_var.set("Cancelling...")

    cancel_button = tk.Button(root, text="Cancel", command=on_cancel, width=12)
    cancel_button.pack(pady=(0, 12))
    root.protocol("WM_DELETE_WINDOW", on_cancel)

    # Work around a macOS Tk bug where some widgets (Canvas, Label) don't
    # actually paint on first show, even though native controls (Button)
    # do. Setting the *same* geometry doesn't trigger a repaint, but an
    # actual pixel-size change does, so briefly resize by a couple pixels
    # and back. Do this a few times shortly after the window appears,
    # since it can take a moment for the window to be fully mapped.
    root.update_idletasks()
    req_w, req_h = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry(f"{req_w}x{req_h}")

    def _bump(step=0):
        try:
            root.update_idletasks()
            w, h = root.winfo_width(), root.winfo_height()
            root.geometry(f"{w + 2}x{h + 2}")
            root.update_idletasks()
            root.geometry(f"{w}x{h}")
        except tk.TclError:
            return
        if step < 3:
            root.after(150, lambda: _bump(step + 1))

    root.after(60, _bump)

    def poll():
        try:
            while True:
                msg = result_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, sent, tot = msg
                    set_progress(sent, tot)
                    pct = (sent / tot * 100) if tot else 100
                    status_var.set(f"{sent // 1024} KB / {tot // 1024} KB ({pct:.0f}%)")
                elif kind == "done":
                    outcome["value"] = ("done", msg[1], msg[2])
                    root.destroy()
                    return
                elif kind == "cancelled":
                    outcome["value"] = ("cancelled",)
                    root.destroy()
                    return
                elif kind == "error":
                    outcome["value"] = ("error", msg[1])
                    root.destroy()
                    return
        except queue.Empty:
            pass
        root.after(100, poll)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    root.after(100, poll)
    root.mainloop()

    result = outcome["value"]
    if result is None or result[0] == "cancelled":
        raise UploadCancelled()
    if result[0] == "error":
        raise result[1]
    _, status, reason = result
    print(f"Upload finished: HTTP {status} {reason}")


# ---------------------------------------------------------------------------

def start_print(host: str, printer_port: int, remote_name: str, timeout: float) -> None:
    print(f"Connecting to printer command port {host}:{printer_port} to start print...")
    with socket.create_connection((host, printer_port), timeout=timeout) as sock:
        sock.sendall(f"M23 {remote_name}\r\n".encode("utf-8"))
        time.sleep(0.5)
        sock.sendall(b"M24\r\n")

        # Give the printer a moment to reply, and print anything it says.
        sock.settimeout(2.0)
        try:
            while True:
                reply = sock.recv(4096)
                if not reply:
                    break
                sys.stdout.write(reply.decode(errors="replace"))
        except socket.timeout:
            pass
    print("\nPrint start command sent.")


def _resolve_remote_name(args) -> str:
    """
    Work out what filename to give the file on the printer's SD card.

    Slicers (PrusaSlicer in particular) call post-processing scripts with
    the path to a *temporary* file, e.g. ".43983.gcode.pp", not the real
    output filename. PrusaSlicer exposes the intended final name via the
    SLIC3R_PP_OUTPUT_NAME environment variable, so prefer that when present.
    """
    if args.remote_name:
        return args.remote_name

    env_name = os.environ.get("SLIC3R_PP_OUTPUT_NAME")
    if env_name:
        return os.path.basename(env_name)

    base = os.path.basename(args.gcode_file)
    if base.endswith(".pp"):
        base = base[:-3]
    # Strip a leading dot PrusaSlicer sometimes adds to its temp filenames
    # (e.g. ".43983.gcode" -> "43983.gcode") so it isn't treated as hidden.
    base = base.lstrip(".")
    return base or "print.gcode"


def main():
    parser = argparse.ArgumentParser(
        prog="mks_send",
        description="Send a .gco/.gcode file to an MKS Robin WiFi / MKS TFT WiFi printer.",
    )
    parser.add_argument(
        "printer_ip",
        help="Printer IP address, optionally with the command port (e.g. 192.168.1.50 or 192.168.1.50:8080)",
    )
    parser.add_argument(
        "gcode_file",
        help="Path to the .gco/.gcode/.g file to send (slicers append this automatically as the last argument)",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"HTTP port used for the file upload (default: {DEFAULT_HTTP_PORT})",
    )
    parser.add_argument(
        "--start-print",
        action="store_true",
        help="After uploading, also tell the printer to start printing the file",
    )
    parser.add_argument(
        "--remote-name",
        default=None,
        help="Filename to use on the printer/SD card (default: derived from the slicer's output name)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Network timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="Use a console progress bar instead of the graphical progress window",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.gcode_file):
        print(f"Error: file not found: {args.gcode_file}", file=sys.stderr)
        sys.exit(1)

    remote_name = _resolve_remote_name(args)

    ext = os.path.splitext(remote_name)[1].lower()
    if ext not in (".gco", ".gcode", ".g"):
        print(f"Warning: unexpected file extension '{ext}' for remote name '{remote_name}' "
              f"(printer expects .gco/.gcode/.g)", file=sys.stderr)

    host, printer_port = parse_address(args.printer_ip, DEFAULT_PRINTER_PORT)

    use_gui = HAS_TK and not args.no_gui

    try:
        if use_gui:
            upload_file_gui(host, args.http_port, args.gcode_file, remote_name, args.timeout)
        else:
            upload_file_console(host, args.http_port, args.gcode_file, remote_name, args.timeout)

        if args.start_print:
            start_print(host, printer_port, remote_name, args.timeout)
    except UploadCancelled:
        print("Upload cancelled by user.", file=sys.stderr)
        sys.exit(4)
    except (OSError, http.client.HTTPException) as e:
        print(f"\nError communicating with printer: {e}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        print(f"\n{e}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
