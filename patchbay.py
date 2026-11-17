#!/usr/bin/env python3
"""
MIDI Patchbay (Python) - verbindt MIDI-ingangen met uitgangen op macOS en Linux.

Alle berichten (incl. sysex, clock, active sensing) worden ongewijzigd doorgestuurd.
De GUI draait lokaal op http://127.0.0.1:8765/ (in elke browser, ook Safari).

Gebruik:
    python3 patchbay.py                 # starten
    python3 patchbay.py --install       # automatisch starten bij inloggen
                                        #   macOS: LaunchAgent, Linux: systemd --user service
    python3 patchbay.py --uninstall     # weer verwijderen

Als zelfstandige app bouwen (macOS .app of Linux-binary): zie build.sh

Vereist: pip install python-rtmidi   (optioneel: pystray pillow voor een menubalk-/systeemvak-icoon)
Linux: eerst  sudo apt install libasound2-dev libjack-jackd2-dev  (voor het bouwen van python-rtmidi)
"""
import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))  # True in een met PyInstaller gebouwde app
CONFIG = Path.home() / ".midi-patchbay.json"
LABEL = "nl.midi-patchbay"
SCAN_INTERVAL = 1.5


OWN_CLIENT = "MIDI Patchbay"


def clean_port_name(name):
    """ALSA (Linux) voegt ' <client>:<poort>' toe, bv. 'Name:Name MIDI 1 20:0'. Die nummers
    veranderen bij opnieuw aansluiten, dus haal ze weg om stabiele sleutels te houden."""
    return re.sub(r"\s+\d+:\d+$", "", name)


def keyed(names):
    """Geef elke poort een stabiele sleutel; identieke namen krijgen ' #2', ' #3', ..."""
    order = sorted(range(len(names)), key=lambda i: (names[i], i))
    seen, result = {}, []
    for idx in order:
        n = names[idx]
        seen[n] = seen.get(n, 0) + 1
        result.append((n if seen[n] == 1 else f"{n} #{seen[n]}", idx))
    return result


class Patchbay:
    def __init__(self):
        import rtmidi
        self.rtmidi = rtmidi
        self.lock = threading.RLock()
        self.ins = {}    # key -> {"port": MidiIn, "count": int}
        self.outs = {}   # key -> {"port": MidiOut, "lock": Lock}
        self.routes = set()
        self.fanout = {}
        self.patch = {}  # uitgang -> patch-instellingen
        self.hidden_ins = set()  # ingangen die niet in de matrix getoond worden
        self._load()
        self.scan()
        threading.Thread(target=self._scan_loop, daemon=True).start()

    # ---------- configuratie ----------
    def _load(self):
        try:
            data = json.loads(CONFIG.read_text())
            if isinstance(data, list):  # oud formaat: alleen routes
                data = {"routes": data}
            self.routes = {(a, b) for a, b in data.get("routes", [])}
            self.patch = dict(data.get("patch", {}))
            self.hidden_ins = set(data.get("hidden_ins", []))
        except Exception:
            self.routes = set()
            self.patch = {}
            self.hidden_ins = set()
        self._rebuild_fanout()

    def _save(self):
        try:
            CONFIG.write_text(json.dumps({"routes": sorted(self.routes), "patch": self.patch,
                                         "hidden_ins": sorted(self.hidden_ins)}, indent=1))
        except Exception as e:
            print("Opslaan mislukt:", e, file=sys.stderr)

    def _rebuild_fanout(self):
        f = {}
        for i, o in self.routes:
            f.setdefault(i, []).append(o)
        self.fanout = f  # atomische vervanging, veilig voor de MIDI-threads

    # ---------- apparaten ----------
    def _scan_loop(self):
        while True:
            time.sleep(SCAN_INTERVAL)
            try:
                self.scan()
            except Exception as e:
                print("Scan-fout:", e, file=sys.stderr)

    def scan(self):
        rt = self.rtmidi
        # Verse instanties, zodat CoreMIDI nieuw aangesloten apparaten ziet
        in_names = [clean_port_name(n) for n in rt.MidiIn().get_ports()]
        out_names = [clean_port_name(n) for n in rt.MidiOut().get_ports()]
        # Sla onze eigen poorten over (op Linux zien andere clients die ook in de lijst)
        want_in = {k: i for k, i in keyed(in_names) if not k.startswith(OWN_CLIENT)}
        want_out = {k: i for k, i in keyed(out_names) if not k.startswith(OWN_CLIENT)}

        with self.lock:
            for key in [k for k in self.ins if k not in want_in]:
                self._close_in(key)
            for key in [k for k in self.outs if k not in want_out]:
                self._close_out(key)

            for key, idx in want_in.items():
                if key in self.ins:
                    continue
                try:
                    m = rt.MidiIn(name="MIDI Patchbay")
                    m.ignore_types(sysex=False, timing=False, active_sense=False)
                    m.open_port(idx)
                    m.set_callback(self._make_callback(key))
                    self.ins[key] = {"port": m, "count": 0}
                except Exception as e:
                    print(f"Kan ingang '{key}' niet openen: {e}", file=sys.stderr)

            for key, idx in want_out.items():
                if key in self.outs:
                    continue
                try:
                    m = rt.MidiOut(name="MIDI Patchbay")
                    m.open_port(idx)
                    self.outs[key] = {"port": m, "lock": threading.Lock()}
                except Exception as e:
                    print(f"Kan uitgang '{key}' niet openen: {e}", file=sys.stderr)

    def _close_in(self, key):
        e = self.ins.pop(key, None)
        if e:
            try:
                e["port"].cancel_callback()
                e["port"].close_port()
            except Exception:
                pass

    def _close_out(self, key):
        e = self.outs.pop(key, None)
        if e:
            try:
                e["port"].close_port()
            except Exception:
                pass

    # ---------- doorsturen ----------
    def _make_callback(self, key):
        def callback(event, data=None):
            msg, _delta = event
            entry = self.ins.get(key)
            if entry:
                entry["count"] += 1
            for o in self.fanout.get(key, ()):
                out = self.outs.get(o)
                if not out:
                    continue
                try:
                    with out["lock"]:
                        out["port"].send_message(msg)
                except Exception as e:
                    print(f"Zenden naar '{o}' mislukt: {e}", file=sys.stderr)
        return callback

    # ---------- API ----------
    def state(self):
        with self.lock:
            return {
                "ins": [{"key": k, "n": v["count"]} for k, v in sorted(self.ins.items())],
                "outs": sorted(self.outs),
                "routes": [list(r) for r in sorted(self.routes)],
                "patch": dict(self.patch),
                "hidden_ins": sorted(self.hidden_ins),
            }

    def toggle(self, i, o):
        with self.lock:
            r = (i, o)
            if r in self.routes:
                self.routes.discard(r)
            else:
                self.routes.add(r)
            self._rebuild_fanout()
            self._save()

    def clear(self):
        with self.lock:
            self.routes.clear()
            self._rebuild_fanout()
            self._save()

    def set_routes(self, routes):
        with self.lock:
            self.routes = {(a, b) for a, b in routes}
            self._rebuild_fanout()
            self._save()

    def set_hidden(self, keys, hidden):
        """Verberg of toon ingangen in de matrix. De verbindingen blijven gewoon actief."""
        with self.lock:
            for k in keys:
                (self.hidden_ins.add if hidden else self.hidden_ins.discard)(k)
            self._save()

    # ---------- patch selecteren ----------
    @staticmethod
    def _clean_patch(cfg):
        def num(v, lo, hi, default):
            try:
                return max(lo, min(hi, int(v)))
            except (TypeError, ValueError):
                return default
        mode = "dx7" if cfg.get("mode") == "dx7" else "pc"
        clean = {
            "mode": mode,
            "channel": num(cfg.get("channel"), 1, 16, 1),
            "number": num(cfg.get("number"), 1, 32 if mode == "dx7" else 128, 1),
            "bank": "crt" if cfg.get("bank") == "crt" else "int",
            "enabled": bool(cfg.get("enabled", False)),
        }
        v = cfg.get("volume")
        clean["volume"] = "" if v in (None, "") else num(v, 0, 127, 100)
        for k in ("msb", "lsb"):
            v = cfg.get(k)
            clean[k] = "" if v in (None, "") else num(v, 0, 127, 0)
        return clean

    def patch_select(self, out_key, cfg, send):
        cfg = self._clean_patch(cfg)
        with self.lock:
            self.patch[out_key] = cfg
            self._save()
        if send:
            self._send_patch(out_key, cfg)

    def set_volume(self, out_key, value, channel):
        """Stuur CC7 (volume) naar een uitgang en bewaar de stand bij de patch-instellingen."""
        try:
            v = max(0, min(127, int(value)))
            ch = max(1, min(16, int(channel))) - 1
        except (TypeError, ValueError):
            return
        with self.lock:
            cfg = self.patch.get(out_key) or self._clean_patch({})
            cfg["volume"] = v
            self.patch[out_key] = cfg
            self._save()
            out = self.outs.get(out_key)
        if out:
            try:
                with out["lock"]:
                    out["port"].send_message([0xB0 | ch, 7, v])
            except Exception as e:
                print(f"Volume sturen naar '{out_key}' mislukt: {e}", file=sys.stderr)

    def _send_patch(self, out_key, cfg):
        out = self.outs.get(out_key)
        if not out:
            return
        ch = cfg["channel"] - 1
        if cfg["mode"] == "dx7":
            # DX7: frontpaneel-knoppen als sysex: F0 43 1n 08 <knop> <7F=in|00=los> F7
            # Eerst Play Internal (0x25) of Play Cartridge (0x26), daarna knop 1-32 (0x00-0x1F).
            base = 0x25 if cfg["bank"] == "int" else 0x26
            n = cfg["number"] - 1
            presses = ((base, 0x7F), (base, 0x00), (n, 0x7F), (n, 0x00))
            msgs = [[0xF0, 0x43, 0x10 | ch, 0x08, b, st, 0xF7] for b, st in presses]
        else:
            msgs = []
            if cfg["msb"] != "":
                msgs.append([0xB0 | ch, 0, cfg["msb"]])
            if cfg["lsb"] != "":
                msgs.append([0xB0 | ch, 32, cfg["lsb"]])
            msgs.append([0xC0 | ch, cfg["number"] - 1])

        def run():
            for i, m in enumerate(msgs):
                if i:
                    time.sleep(0.03)
                try:
                    with out["lock"]:
                        out["port"].send_message(m)
                except Exception as e:
                    print(f"Patch sturen naar '{out_key}' mislukt: {e}", file=sys.stderr)

        threading.Thread(target=run, daemon=True).start()


# ---------- HTTP ----------
def make_handler(bay):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/api/state":
                self._send(200, json.dumps(bay.state()))
            elif self.path in ("/", "/index.html"):
                self._send(200, HTML, "text/html; charset=utf-8")
            else:
                self._send(404, "{}")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._send(400, "{}")
            # Eenvoudige bescherming tegen cross-site requests
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}",
                                         f"http://localhost:{self.server.server_port}"):
                return self._send(403, "{}")
            if self.path == "/api/toggle":
                bay.toggle(body["in"], body["out"])
            elif self.path == "/api/clear":
                bay.clear()
            elif self.path == "/api/rescan":
                bay.scan()
            elif self.path == "/api/hidden":
                bay.set_hidden([str(k) for k in body.get("keys", [])], bool(body.get("hidden")))
            elif self.path == "/api/volume":
                bay.set_volume(body.get("out", ""), body.get("value"), body.get("channel", 1))
            elif self.path == "/api/patch":
                bay.patch_select(body["out"], body.get("cfg", {}), bool(body.get("send")))
            elif self.path == "/api/import":
                bay.set_routes(body["routes"])
            else:
                return self._send(404, "{}")
            self._send(200, json.dumps(bay.state()))

    return Handler


# ---------- Automatisch starten ----------
SYSTEMD_UNIT = Path.home() / ".config/systemd/user/midi-patchbay.service"
LAUNCH_AGENT = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG_MACOS = Path.home() / "Library/Logs/midi-patchbay.log"


def service_command(port):
    """Het commando waarmee een service/LaunchAgent de patchbay start (zonder browser te openen)."""
    if FROZEN:  # gebouwde app: het uitvoerbare bestand is de patchbay zelf
        return [sys.executable, "--port", str(port), "--no-open"]
    return [sys.executable, str(Path(__file__).resolve()), "--port", str(port), "--no-open"]


def autostart_installed():
    if sys.platform == "darwin":
        return LAUNCH_AGENT.exists()
    if sys.platform.startswith("linux"):
        return SYSTEMD_UNIT.exists()
    return False


def install(port):
    if sys.platform == "darwin":
        return install_macos(port)
    if sys.platform.startswith("linux"):
        return install_linux(port)
    sys.exit("Automatisch starten wordt alleen op macOS en Linux ondersteund.")


def uninstall():
    if sys.platform == "darwin":
        return uninstall_macos()
    if sys.platform.startswith("linux"):
        return uninstall_linux()
    sys.exit("Niet ondersteund op dit platform.")


def launch_agent_plist(port):
    return {
        "Label": LABEL,
        "ProgramArguments": service_command(port),
        "RunAtLoad": True,
        # Alleen herstarten bij een crash. Een tweede instantie die direct stopt omdat de poort
        # al bezet is (afsluitcode 0) mag niet eindeloos opnieuw gestart worden.
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(LOG_MACOS),
        "StandardErrorPath": str(LOG_MACOS),
    }


def install_macos(port):
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    with open(LAUNCH_AGENT, "wb") as f:
        plistlib.dump(launch_agent_plist(port), f)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], stderr=subprocess.DEVNULL)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(LAUNCH_AGENT)], check=True)
    print(f"Geinstalleerd. Start nu automatisch bij inloggen. GUI: http://127.0.0.1:{port}/")
    print(f"Log: {LOG_MACOS}")


def uninstall_macos():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], stderr=subprocess.DEVNULL)
    LAUNCH_AGENT.unlink(missing_ok=True)
    print("LaunchAgent verwijderd.")


def systemd_unit_text(port):
    exec_start = " ".join('"' + part.replace('"', '\\"') + '"' for part in service_command(port))
    return (
        "[Unit]\n"
        "Description=MIDI Patchbay\n"
        "\n"
        "[Service]\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_linux(port):
    SYSTEMD_UNIT.parent.mkdir(parents=True, exist_ok=True)
    SYSTEMD_UNIT.write_text(systemd_unit_text(port))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "midi-patchbay.service"], check=True)
    print(f"Geinstalleerd. Start nu automatisch bij inloggen. GUI: http://127.0.0.1:{port}/")
    print("Log bekijken: journalctl --user -u midi-patchbay -f")
    print("Ook starten zonder in te loggen (bij boot): sudo loginctl enable-linger $USER")


def uninstall_linux():
    subprocess.run(["systemctl", "--user", "disable", "--now", "midi-patchbay.service"],
                   stderr=subprocess.DEVNULL)
    SYSTEMD_UNIT.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], stderr=subprocess.DEVNULL)
    print("systemd-service verwijderd.")


# ---------- Browser, log en menubalk-icoon ----------
def open_url(url):
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"Browser openen mislukt: {e} (ga zelf naar {url})", file=sys.stderr)


def redirect_output_to_log():
    """Een gebouwde app heeft geen terminal: schrijf meldingen naar een logbestand."""
    if sys.stdout is not None and sys.stdout.isatty():
        return
    log = LOG_MACOS if sys.platform == "darwin" else Path.home() / ".midi-patchbay.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        f = open(log, "a", buffering=1)
        sys.stdout = sys.stderr = f
    except Exception:
        pass


def make_tray_icon(Image, ImageDraw):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 61, 61), radius=14, fill=(29, 32, 38, 255), outline=(61, 220, 151, 255), width=3)
    # vijf punten in een boog, zoals de pinnen van een MIDI-DIN-connector
    for x, y in ((14, 34), (20, 20), (32, 14), (44, 20), (50, 34)):
        d.ellipse((x - 4, y - 4, x + 4, y + 4), fill=(61, 220, 151, 255))
    return img


def run_tray(url, port, stop):
    """Toon een icoon in de menubalk/het systeemvak. Blokkeert tot 'Stop'.
    Geeft False terug als dat niet beschikbaar is (bibliotheek of grafische omgeving ontbreekt)."""
    try:
        import pystray
        from PIL import Image, ImageDraw
        icon_img = make_tray_icon(Image, ImageDraw)
    except Exception as e:
        print(f"Geen menubalk-icoon ({e}).", file=sys.stderr)
        return False

    def on_open(icon, item):
        open_url(url)

    def on_autostart(icon, item):
        try:
            if autostart_installed():
                uninstall()
            else:
                install(port)
        except BaseException as e:  # SystemExit/CalledProcessError mogen de app niet stoppen
            print(f"Automatisch starten wijzigen mislukt: {e}", file=sys.stderr)

    def on_stop(icon, item):
        icon.stop()

    items = [pystray.MenuItem("Open patchbay", on_open, default=True)]
    if sys.platform == "darwin" or sys.platform.startswith("linux"):
        items.append(pystray.MenuItem("Start bij inloggen", on_autostart,
                                      checked=lambda item: autostart_installed()))
    items.append(pystray.MenuItem("Stop", on_stop))
    try:
        pystray.Icon("midi-patchbay", icon_img, "MIDI Patchbay", pystray.Menu(*items)).run()
    except Exception as e:
        print(f"Menubalk-icoon starten mislukt: {e}", file=sys.stderr)
        return False
    stop()
    return True


def main():
    ap = argparse.ArgumentParser(description="MIDI Patchbay")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--install", action="store_true", help="automatisch starten bij inloggen")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--open", action="store_true", help="open de GUI in de browser")
    ap.add_argument("--no-open", action="store_true", help="open de browser niet (ook niet in de app)")
    ap.add_argument("--no-tray", action="store_true", help="geen menubalk-/systeemvak-icoon")
    args = ap.parse_args()

    if args.install:
        return install(args.port)
    if args.uninstall:
        return uninstall()

    if FROZEN:
        redirect_output_to_log()
    url = f"http://127.0.0.1:{args.port}/"
    do_open = (args.open or FROZEN) and not args.no_open

    try:
        bay = Patchbay()
    except ImportError:
        sys.exit("python-rtmidi ontbreekt. Installeer met: pip install python-rtmidi")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(bay))
    except OSError:
        # Poort bezet: waarschijnlijk draait de patchbay al. Open dan alleen de bestaande.
        print(f"Poort {args.port} is al in gebruik; de patchbay draait waarschijnlijk al op {url}")
        if do_open:
            open_url(url)
        return

    print(f"MIDI Patchbay draait op {url}  (Ctrl+C om te stoppen)")
    if do_open:
        open_url(url)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop():
        server.shutdown()

    shown = False
    # Een icoon is alleen zinvol in een gebouwde app of als erom gevraagd is via een grafische sessie
    if not args.no_tray and (FROZEN or sys.stdout is None or not sys.stdout.isatty()):
        shown = run_tray(url, args.port, stop)
    if not shown:
        try:
            while thread.is_alive():
                thread.join(0.5)
        except KeyboardInterrupt:
            server.shutdown()


HTML = r"""<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MIDI Patchbay</title>
<style>
  :root{--bg:#14161a;--panel:#1d2026;--line:#2b3038;--text:#e6e8eb;--dim:#8a919c;--accent:#3ddc97;--warn:#f5a524;--err:#ff5c5c}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;padding:24px}
  h1{font-size:18px;margin:0 0 4px}
  .sub{color:var(--dim);margin-bottom:18px}
  .bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;align-items:center}
  button{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:7px 12px;font:inherit;cursor:pointer}
  button:hover{border-color:var(--dim)}
  .status{margin-left:auto;color:var(--dim)} .status.err{color:var(--err)}
  .wrap{overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
  table{border-collapse:separate;border-spacing:4px}
  th{font-weight:500;font-size:12px;color:var(--dim)}
  th.col{height:150px;vertical-align:bottom;white-space:nowrap;padding:0}
  th.col div{writing-mode:vertical-rl;transform:rotate(180deg);padding:4px;max-height:150px;overflow:hidden;text-overflow:ellipsis}
  th.row{text-align:right;padding-right:10px;color:var(--text);font-size:13px;white-space:nowrap}
  .led{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3a3f48;margin-right:8px;transition:background .08s}
  .led.on{background:var(--accent);box-shadow:0 0 6px var(--accent)}
  td.cell{width:34px;height:34px;padding:0}
  td.cell button{width:34px;height:34px;padding:0;border-radius:6px;background:#242830;border:1px solid var(--line)}
  td.cell button:hover{border-color:var(--accent)}
  td.cell button.on{background:var(--accent);border-color:var(--accent)}
  td.cell button.loop{border-style:dashed;border-color:#4a4f58}
  td.cell button.loop.on{background:var(--warn);border-color:var(--warn)}
  .hint{color:var(--dim);margin-top:14px;font-size:12px}
  .legend span{margin-right:14px}
  .empty{color:var(--dim);padding:30px;text-align:center}
  a{color:var(--dim)}
  [hidden]{display:none!important}
  .menu{position:relative}
  .pop{position:absolute;top:100%;left:0;z-index:10;margin-top:6px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px;min-width:250px;max-height:60vh;overflow:auto;box-shadow:0 8px 24px rgba(0,0,0,.45)}
  .pop .links{display:flex;gap:14px;margin-bottom:6px;font-size:12px}
  .pop label{display:flex;gap:8px;align-items:center;padding:3px 0;cursor:pointer;font-size:13px}
  .layout{display:flex;gap:16px;align-items:flex-start;flex-wrap:nowrap}
  .layout .wrap{flex:1 1 0;min-width:0}
  .side{flex:0 0 310px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
  .side:empty{display:none}
  .ptitle{display:flex;gap:6px;align-items:center;margin-bottom:6px}
  .ptitle .pname{flex:1;margin-bottom:0}
  .pclose{padding:0 8px;line-height:1.5;color:var(--dim)}
  .addrow select{width:100%}
  .prow input.vol{flex:1;min-width:90px;padding:0;accent-color:var(--accent)}
  .volv{min-width:30px;text-align:right;font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums}
  .noneyet{color:var(--dim);font-size:12px;margin:10px 0}
  .side h2{font-size:14px;margin:0 0 12px}
  .pcard{border-top:1px solid var(--line);padding:10px 0}
  .side h2 + .pcard{border-top:0;padding-top:0}
  .side h2:not(:first-child){margin-top:18px}
  .pname{font-weight:600;font-size:13px;margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .prow{display:flex;gap:6px;align-items:center;margin-top:6px;flex-wrap:wrap}
  .prow label{color:var(--dim);font-size:12px;display:flex;gap:4px;align-items:center}
  select,input{background:#242830;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:5px 6px;font:inherit}
  input.num{width:64px;text-align:center}
  .prow input.msb,.prow input.lsb{width:52px}
  .prow .send{background:var(--accent);color:#06140d;border-color:var(--accent);font-weight:600;min-width:58px}
  .prow .prev,.prow .next{padding:5px 9px}
</style>
</head>
<body>
<h1>MIDI Patchbay</h1>
<div class="sub">Klik op een kruispunt om een ingang (rij) met een uitgang (kolom) te verbinden. Alle bytes, inclusief sysex, worden ongewijzigd doorgegeven.</div>
<div class="bar">
  <span class="menu">
    <button id="inbtn">Ingangen tonen ▾</button>
    <div class="pop" id="pop" hidden></div>
  </span>
  <button id="rescan" title="Zoek opnieuw naar aangesloten MIDI-apparaten">Opnieuw scannen</button>
  <button id="clear">Alles ontkoppelen</button>
  <button id="export">Exporteer</button>
  <button id="import">Importeer</button>
  <input id="file" type="file" accept="application/json" hidden>
  <span class="status" id="status">Verbinden…</span>
</div>
<div class="layout">
  <div class="wrap" id="wrap"><div class="empty">Laden…</div></div>
  <aside class="side" id="side"></aside>
</div>
<div class="hint legend">
  <span>🟩 verbonden</span>
  <span>🟧 verbonden met zichzelf (kan een loop veroorzaken)</span>
  <span>🟥 opgeslagen verbinding, apparaat nu niet aanwezig</span>
</div>
<div class="hint"><b>Patch selecteren:</b> "Program change" stuurt (optioneel bank-select CC0/CC32 en) een program change, patch 1 = PC 0. "DX7 (sysex)" drukt de knoppen Internal/Cartridge en daarna knop 1–32 in via sysex, voor een DX7 die geen program changes accepteert. Voeg met "+ Uitgang toevoegen" alleen de uitgangen toe waarvoor je een patch wilt kiezen; met × haal je er een weer weg.</div>
<div class="hint"><b>Volume:</b> de schuif in elk uitgang-kaartje stuurt CC7 (volume) naar die uitgang, op het gekozen kanaal. Er wordt pas iets verstuurd zodra je de schuif beweegt; het apparaat zelf kan een andere stand hebben, dus "–" betekent: nog niet ingesteld. Dubbelklik = 100.</div>
<div class="hint">De service draait ook zonder open browser. Verbindingen worden onthouden en hersteld zodra een apparaat weer wordt aangesloten.</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let sig = '', last = {}, leds = {}, current = null;

async function api(path, body){
  const r = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)});
  return r.json();
}
function setStatus(t, err){ const s = $('status'); s.textContent = t; s.className = 'status' + (err ? ' err' : ''); }

function render(s){
  current = s; leds = {};
  const wrap = $('wrap');
  if (!s.ins.length && !s.outs.length){
    wrap.innerHTML = '<div class="empty">Geen MIDI-apparaten gevonden. Sluit een apparaat aan via USB.</div>';
    return;
  }
  const routes = new Set(s.routes.map(r => r.join('\u0001')));
  const inKeys = new Set(s.ins.map(x => x.key)), outKeys = new Set(s.outs);
  const hid = new Set(s.hidden_ins || []);
  const visible = s.ins.filter(i => !hid.has(i.key));
  const tbl = document.createElement('table');
  const head = tbl.insertRow();
  head.insertCell().outerHTML = '<th></th>';
  for (const o of s.outs) head.insertCell().outerHTML = `<th class="col"><div title="${esc(o)}">${esc(o)}</div></th>`;
  for (const i of visible){
    const tr = tbl.insertRow();
    const th = document.createElement('th'); th.className = 'row';
    const led = document.createElement('span'); led.className = 'led'; leds[i.key] = led;
    th.append(led, i.key); tr.appendChild(th);
    for (const o of s.outs){
      const td = tr.insertCell(); td.className = 'cell';
      const b = document.createElement('button');
      if (i.key === o) b.classList.add('loop');
      if (routes.has(i.key + '\u0001' + o)) b.classList.add('on');
      b.title = `${i.key} → ${o}`;
      b.onclick = async () => update(await api('/api/toggle', {in: i.key, out: o}));
      td.appendChild(b);
    }
  }
  wrap.innerHTML = ''; wrap.appendChild(tbl);
  if (s.ins.length && !visible.length){
    const d = document.createElement('div'); d.className = 'hint';
    d.textContent = 'Alle ingangen zijn verborgen. Kies er een via "Ingangen tonen".'; wrap.appendChild(d);
  }
  const missing = s.routes.filter(([i,o]) => !inKeys.has(i) || !outKeys.has(o));
  if (missing.length){
    const d = document.createElement('div'); d.className = 'hint';
    d.innerHTML = 'Opgeslagen, nu niet beschikbaar:<br>';
    for (const [i,o] of missing){
      const span = document.createElement('span');
      span.innerHTML = `<span style="color:var(--err)">${esc(i)} → ${esc(o)}</span> `;
      const a = document.createElement('a'); a.href = '#'; a.textContent = 'verwijder';
      a.onclick = async ev => { ev.preventDefault(); update(await api('/api/toggle', {in: i, out: o})); };
      span.appendChild(a); d.appendChild(span); d.appendChild(document.createElement('br'));
    }
    wrap.appendChild(d);
  }
  const active = s.routes.length - missing.length;
  setStatus(`${visible.length}/${s.ins.length} ingangen · ${s.outs.length} uitgangen · ${active} verbindingen actief`);
}

let psig = '', pcfg = {};

async function setHidden(keys, hidden){ update(await api('/api/hidden', {keys, hidden})); }

function buildPop(){
  const pop = $('pop');
  if (pop.hidden || !current) return;
  const top = pop.scrollTop, hid = new Set(current.hidden_ins || []);
  pop.innerHTML = '';
  const links = document.createElement('div'); links.className = 'links';
  for (const [t, hide] of [['Alles tonen', false], ['Alles verbergen', true]]){
    const a = document.createElement('a'); a.href = '#'; a.textContent = t;
    a.onclick = ev => { ev.preventDefault(); setHidden(current.ins.map(x => x.key), hide); };
    links.appendChild(a);
  }
  pop.appendChild(links);
  if (!current.ins.length){ const d = document.createElement('div'); d.className = 'hint'; d.textContent = 'Geen ingangen gevonden.'; pop.appendChild(d); }
  for (const i of current.ins){
    const lab = document.createElement('label');
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = !hid.has(i.key);
    cb.onchange = () => setHidden([i.key], !cb.checked);
    lab.append(cb, i.key); pop.appendChild(lab);
  }
  pop.scrollTop = top;
}
$('inbtn').onclick = e => { e.stopPropagation(); const p = $('pop'); p.hidden = !p.hidden; buildPop(); };
document.addEventListener('click', e => { if (!e.target.closest('.menu')) $('pop').hidden = true; });
const PCDEF = {mode:'pc', channel:1, number:1, bank:'int', msb:'', lsb:'', volume:''};

function renderPatch(s){
  const box = $('side'); box.innerHTML = '';
  if (!s.outs.length) return;
  const patch = s.patch || {};
  const isOn = o => !!(patch[o] && patch[o].enabled);
  const enabled = s.outs.filter(isOn), rest = s.outs.filter(o => !isOn(o));
  const chOpts = Array.from({length:16}, (_, i) => `<option value="${i+1}">${i+1}</option>`).join('');

  const h = document.createElement('h2'); h.textContent = 'Patch selecteren'; box.appendChild(h);

  if (rest.length){
    const row = document.createElement('div'); row.className = 'prow addrow';
    const sel = document.createElement('select');
    sel.innerHTML = '<option value="">+ Uitgang toevoegen…</option>' +
      rest.map(o => `<option value="${esc(o)}">${esc(o)}</option>`).join('');
    sel.onchange = () => { if (sel.value) setEnabled(sel.value, true); };
    row.appendChild(sel); box.appendChild(row);
  }
  if (!enabled.length){
    const n = document.createElement('div'); n.className = 'noneyet';
    n.textContent = 'Voeg een uitgang toe om een patch te kiezen.'; box.appendChild(n);
  }

  async function setEnabled(o, on){
    const c = pcfg[o] = Object.assign({}, PCDEF, patch[o] || {}, {enabled: on});
    try {
      const st = await api('/api/patch', {out: o, cfg: c, send: false});
      psig = JSON.stringify(st.outs); renderPatch(st);
    } catch { setStatus('Opslaan mislukt', true); }
  }

  for (const o of enabled){
    const c = pcfg[o] = Object.assign({}, PCDEF, patch[o], {enabled: true});
    const card = document.createElement('div'); card.className = 'pcard';
    card.innerHTML = `
      <div class="ptitle"><div class="pname" title="${esc(o)}">${esc(o)}</div>
        <button class="pclose" title="Verwijder uit lijst">×</button></div>
      <div class="prow">
        <select class="mode"><option value="pc">Program change</option><option value="dx7">DX7 (sysex)</option></select>
        <label>Kanaal <select class="ch">${chOpts}</select></label>
      </div>
      <div class="prow">
        <button class="prev" title="Vorige patch">◀</button>
        <input class="num" type="number" step="1" min="1">
        <button class="next" title="Volgende patch">▶</button>
        <button class="send">Stuur</button>
      </div>
      <div class="prow pcx">
        <label>Bank MSB <input class="msb" type="number" min="0" max="127"></label>
        <label>LSB <input class="lsb" type="number" min="0" max="127"></label>
      </div>
      <div class="prow volrow">
        <label style="flex:1">Volume (CC7) <input class="vol" type="range" min="0" max="127" step="1" title="Stuurt CC7 naar deze uitgang (dubbelklik = 100)"> <span class="volv"></span></label>
      </div>
      <div class="prow dxx">
        <label>Geheugen <select class="bank"><option value="int">Internal</option><option value="crt">Cartridge</option></select></label>
      </div>`;
    const q = sel => card.querySelector(sel);
    const sync = () => {
      const max = c.mode === 'dx7' ? 32 : 128;
      c.number = Math.max(1, Math.min(max, parseInt(c.number) || 1));
      q('.mode').value = c.mode; q('.ch').value = c.channel;
      q('.num').max = max; q('.num').value = c.number;
      q('.msb').value = c.msb; q('.lsb').value = c.lsb; q('.bank').value = c.bank;
      q('.vol').value = c.volume === '' ? 100 : c.volume;
      q('.volv').textContent = c.volume === '' ? '–' : c.volume;
      q('.pcx').hidden = c.mode !== 'pc'; q('.dxx').hidden = c.mode !== 'dx7';
    };
    const push = async send => {
      sync();
      try { await api('/api/patch', {out: o, cfg: c, send}); } catch { setStatus('Opslaan mislukt', true); return; }
      if (send){ const b = q('.send'); b.textContent = '✓'; setTimeout(() => b.textContent = 'Stuur', 700); }
    };
    let vtimer = null, vlast = 0;
    const sendVol = () => {
      vlast = Date.now(); vtimer = null;
      api('/api/volume', {out: o, value: c.volume, channel: c.channel}).catch(() => setStatus('Volume sturen mislukt', true));
    };
    const setVol = v => {
      c.volume = +v; q('.volv').textContent = v;
      if (vtimer) return;
      vtimer = setTimeout(sendVol, Math.max(0, 40 - (Date.now() - vlast)));
    };
    q('.vol').oninput = e => setVol(e.target.value);
    q('.vol').ondblclick = () => { q('.vol').value = 100; setVol(100); };
    q('.pclose').onclick = () => setEnabled(o, false);
    q('.mode').onchange = e => { c.mode = e.target.value; push(false); };
    q('.ch').onchange = e => { c.channel = +e.target.value; push(false); };
    q('.bank').onchange = e => { c.bank = e.target.value; push(false); };
    q('.msb').onchange = e => { c.msb = e.target.value; push(false); };
    q('.lsb').onchange = e => { c.lsb = e.target.value; push(false); };
    q('.num').onchange = e => { c.number = e.target.value; push(false); };
    q('.num').onkeydown = e => { if (e.key === 'Enter'){ c.number = e.target.value; push(true); } };
    q('.prev').onclick = () => { c.number = (parseInt(q('.num').value) || 1) - 1; push(true); };
    q('.next').onclick = () => { c.number = (parseInt(q('.num').value) || 1) + 1; push(true); };
    q('.send').onclick = () => { c.number = q('.num').value; push(true); };
    sync();
    box.appendChild(card);
  }
}

function update(s){
  const nsig = JSON.stringify([s.ins.map(x => x.key), s.outs, s.routes, s.hidden_ins]);
  if (nsig !== sig){ sig = nsig; render(s); buildPop(); }
  const np = JSON.stringify(s.outs);
  if (np !== psig){ psig = np; renderPatch(s); }
  for (const i of s.ins){
    if (last[i.key] !== undefined && last[i.key] !== i.n && leds[i.key]){
      const led = leds[i.key]; led.classList.add('on');
      clearTimeout(led._t); led._t = setTimeout(() => led.classList.remove('on'), 90);
    }
    last[i.key] = i.n;
  }
}

async function poll(){
  try { update(await api('/api/state')); }
  catch { setStatus('Geen verbinding met de patchbay-service', true); }
  setTimeout(poll, 150);
}

$('rescan').onclick = async () => { setStatus('Scannen…'); try { const st = await api('/api/rescan', {}); sig = ''; psig = ''; update(st); } catch { setStatus('Scannen mislukt', true); } };
$('clear').onclick = async () => { if (confirm('Alle verbindingen verwijderen?')) update(await api('/api/clear', {})); };
$('export').onclick = () => {
  const blob = new Blob([JSON.stringify(current ? current.routes : [], null, 2)], {type:'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'midi-patch.json'; a.click();
};
$('import').onclick = () => $('file').click();
$('file').onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  try { update(await api('/api/import', {routes: JSON.parse(await f.text())})); } catch { alert('Ongeldig bestand'); }
  e.target.value = '';
};
poll();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
