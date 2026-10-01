#!/usr/bin/env python3
"""국내선 항공권 감시기 (네이버 항공권 + 여기어때) - 웹 UI 버전.

감시 스레드와 설정 UI 서버를 한 프로세스에서 띄운다.
사용법: python3 app.py [--port 8765]  →  http://localhost:8765
      (윈도우: start.bat 실행 또는 python app.py)
"""
import argparse
import json
import random
import sys
import threading
import uuid
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from watch import AIRLINE_NAMES, SEAT_CLASS, SOURCES, alarm, describe, search

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
INDEX_HTML = ROOT / "ui" / "index.html"

WATCH_DEFAULTS = {
    "enabled": True, "name": "", "from": "GMP", "to": "CJU", "date": "", "adults": 1,
    "airlines": [], "flight_numbers": [], "depart_after": None, "depart_before": None,
    "max_price": None,
}


def normalize_config(cfg):
    out = {"interval_seconds": max(10, int(cfg.get("interval_seconds") or 60)), "watches": []}
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
        "prices": f["prices"], "source": f["source"],
    }


class Watcher:
    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.config = normalize_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        self._save()  # id 부여 결과 저장
        self.status = {}  # watch id -> {fp, prices, flights, checked_at, error}
        self.alerts = deque(maxlen=100)
        self.next_check_at = None

    def _save(self):
        CONFIG_PATH.write_text(json.dumps(self.config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

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
                "status": {k: {kk: vv for kk, vv in v.items() if kk not in ("fp", "prices", "by_source", "ok_sources")}
                           for k, v in self.status.items()},
                "alerts": list(self.alerts),
                "next_check_at": self.next_check_at,
            }

    def check_one(self, w):
        # 조건이 바뀌면 처음 조회처럼 취급해서 기존 편들로 알림이 쏟아지지 않게 한다
        fp = json.dumps({k: v for k, v in w.items() if k not in ("name", "enabled")}, sort_keys=True)
        now = datetime.now().isoformat(timespec="seconds")
        with self.lock:
            prev = self.status.get(w["id"], {})
        first = prev.get("fp") != fp
        merged, by_source, errors, partial = search(w, None if first else prev.get("by_source"))
        error = " / ".join(f"{SOURCES[s]['label']}: {e}" for s, e in {**partial, **errors}.items()) or None
        if len(errors) == len(SOURCES):
            with self.lock:
                self.status.setdefault(w["id"], {})
                self.status[w["id"]].update(checked_at=now, error=error)
            return

        # 이번에 처음 성공한 사이트가 있으면 그 사이트 결과는 기준값으로만 쓰고 알림은 생략한다
        ok_sources = (set() if first else set(prev.get("ok_sources", ()))) | (set(SOURCES) - set(errors))
        baseline = first or not ok_sources.issubset(prev.get("ok_sources", ()))
        prices = {} if first else prev.get("prices", {})
        changed = [] if baseline else [
            k for k, f in merged.items() if k not in prices or f["minFare"] < prices[k]
        ]
        ordered = sorted(merged.items(), key=lambda kv: kv[1]["segment"]["departure"]["time"])
        label = w["name"] or f"{w['from']}→{w['to']} {w['date'][5:]}"
        with self.lock:
            self.status[w["id"]] = {
                "fp": fp, "by_source": by_source, "ok_sources": sorted(ok_sources),
                "prices": {k: f["minFare"] for k, f in merged.items()},
                "flights": [flight_info(f, k in changed) for k, f in ordered],
                "checked_at": now, "error": error,
            }
            for k in changed:
                self.alerts.appendleft({"at": now, "watch": label, "text": describe(merged[k])})
        if changed:
            changed.sort(key=lambda k: merged[k]["segment"]["departure"]["time"])
            alarm(f"✈️ {label} 새 항공권 {len(changed)}편!", [merged[k] for k in changed], w)
        elif first and merged:
            alarm(f"✈️ {label} {len(merged)}편 있음", [f for _, f in ordered], w)

    def run(self):
        while True:
            self.wake.clear()
            with self.lock:
                watches = [dict(w) for w in self.config["watches"] if w["enabled"]]
                interval = self.config["interval_seconds"]
            # 항목별 조회가 수 초씩 걸려서 동시에 돌린다
            with ThreadPoolExecutor(max_workers=max(1, len(watches))) as ex:
                list(ex.map(self.check_one, watches))
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
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
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
