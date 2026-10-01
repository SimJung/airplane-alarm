#!/usr/bin/env python3
"""네이버 항공권 감시기 - 웹 UI 버전.

감시 스레드와 설정 UI 서버를 한 프로세스에서 띄운다.
사용법: python3 app.py [--port 8765]  →  http://localhost:8765
"""
import argparse
import json
import random
import threading
import uuid
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from watch import AIRLINE_NAMES, SEAT_CLASS, alarm, best_by_flight, describe, fetch_flights, matches, page_url

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
INDEX_HTML = ROOT / "ui" / "index.html"

WATCH_DEFAULTS = {
    "enabled": True, "name": "", "from": "GMP", "to": "CJU", "date": "", "adults": 1,
    "airlines": [], "flight_numbers": [], "depart_after": None, "depart_before": None,
    "max_price": None,
}


def normalize_config(cfg):
    out = {"interval_seconds": max(30, int(cfg.get("interval_seconds") or 60)), "watches": []}
    for w in cfg.get("watches", []):
        w = {**WATCH_DEFAULTS, **w}
        w.pop("min_seats", None)  # 예전 설정 파일 호환
        w["id"] = w.get("id") or uuid.uuid4().hex[:8]
        w["from"], w["to"] = w["from"].upper(), w["to"].upper()
        if w["from"] == w["to"]:
            raise ValueError(f"출발지와 도착지가 같음: {w['from']}")
        datetime.strptime(w["date"], "%Y-%m-%d")
        w["adults"] = max(1, int(w["adults"]))
        w["max_price"] = int(w["max_price"]) if w["max_price"] else None
        w["depart_after"] = w["depart_after"] or None
        w["depart_before"] = w["depart_before"] or None
        w["flight_numbers"] = [n.strip().upper() for n in w["flight_numbers"] if n.strip()]
        out["watches"].append(w)
    return out


def flight_info(f, is_new):
    seg = f["segment"]
    code = seg["airlineCode"]
    return {
        "dep": seg["departure"]["time"], "arr": seg["arrival"]["time"],
        "airline": AIRLINE_NAMES.get(code, code), "flight": f"{code}{seg['flightNumber']}",
        "seat_class": SEAT_CLASS.get(f.get("seatClass"), f.get("seatClass")),
        "price": f["minFare"], "seats": f.get("seatCount"), "new": is_new,
    }


class Watcher:
    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.config = normalize_config(json.loads(CONFIG_PATH.read_text()))
        self._save()  # id 부여 결과 저장
        self.status = {}  # watch id -> {fp, prices, flights, checked_at, error}
        self.alerts = deque(maxlen=100)
        self.next_check_at = None

    def _save(self):
        CONFIG_PATH.write_text(json.dumps(self.config, ensure_ascii=False, indent=2) + "\n")

    def set_config(self, cfg):
        cfg = normalize_config(cfg)
        with self.lock:
            self.config = cfg
            self._save()
        self.wake.set()
        return cfg

    def snapshot(self):
        with self.lock:
            return {
                "config": self.config,
                "status": {k: {kk: vv for kk, vv in v.items() if kk not in ("fp", "prices")}
                           for k, v in self.status.items()},
                "alerts": list(self.alerts),
                "next_check_at": self.next_check_at,
            }

    def check_one(self, w):
        # 조건이 바뀌면 처음 조회처럼 취급해서 기존 편들로 알림이 쏟아지지 않게 한다
        fp = json.dumps({k: v for k, v in w.items() if k not in ("name", "enabled")}, sort_keys=True)
        now = datetime.now().isoformat(timespec="seconds")
        try:
            best = best_by_flight(f for f in fetch_flights(w) if matches(f, w))
        except Exception as e:
            with self.lock:
                self.status.setdefault(w["id"], {})
                self.status[w["id"]].update(checked_at=now, error=str(e))
            return
        with self.lock:
            prev = self.status.get(w["id"], {})
            first = prev.get("fp") != fp
            prices = {} if first else prev.get("prices", {})
            changed = [k for k, f in best.items() if not first and (k not in prices or f["minFare"] < prices[k])]
            ordered = sorted(best.items(), key=lambda kv: kv[1]["segment"]["departure"]["time"])
            self.status[w["id"]] = {
                "fp": fp, "prices": {k: f["minFare"] for k, f in best.items()},
                "flights": [flight_info(f, k in changed) for k, f in ordered],
                "checked_at": now, "error": None,
            }
            label = w["name"] or f"{w['from']}→{w['to']} {w['date'][5:]}"
            for k in changed:
                self.alerts.appendleft({"at": now, "watch": label, "text": describe(best[k])})
        if changed:
            changed.sort(key=lambda k: best[k]["segment"]["departure"]["time"])
            alarm(f"✈️ {label} 새 항공권 {len(changed)}편!", [describe(best[k]) for k in changed], page_url(w))
        elif first and best:
            alarm(f"✈️ {label} {len(best)}편 있음", [describe(f) for _, f in ordered], page_url(w))

    def run(self):
        while True:
            self.wake.clear()
            with self.lock:
                watches = [dict(w) for w in self.config["watches"] if w["enabled"]]
                interval = self.config["interval_seconds"]
            for w in watches:
                self.check_one(w)
            wait = interval + random.uniform(0, interval * 0.2)
            with self.lock:
                self.next_check_at = datetime.fromtimestamp(datetime.now().timestamp() + wait).isoformat(timespec="seconds")
            self.wake.wait(wait)


def make_handler(watcher):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send(200, watcher.snapshot())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path == "/api/config":
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    self._send(200, watcher.set_config(body))
                except (ValueError, KeyError, TypeError) as e:
                    self._send(400, {"error": str(e)})
            elif self.path == "/api/check":
                watcher.wake.set()
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="브라우저 자동으로 열지 않기")
    args = ap.parse_args()

    watcher = Watcher()
    threading.Thread(target=watcher.run, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(watcher))
    url = f"http://localhost:{args.port}"
    print(f"항공권 감시 중 — 설정 UI: {url}  (Ctrl+C 로 종료)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
