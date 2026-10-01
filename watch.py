#!/usr/bin/env python3
"""네이버 항공권(국내선) 감시기.

config.json 의 조건에 맞는 항공편이 새로 생기거나 더 싸지면 macOS 알림을 띄운다.
사용법: python3 watch.py [--once] [--config config.json]
"""
import argparse
import json
import random
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

API_URL = "https://flight-api.naver.com/flight/domestic/searchFlights"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
AIRLINE_NAMES = {}  # 응답의 airlinesCodeMap 으로 채움
SEAT_CLASS = {"Y": "일반석", "D": "할인석", "L": "특가석", "C": "비즈니스", "F": "일등석"}


def page_url(w):
    d = w["date"].replace("-", "")
    return (f"https://flight.naver.com/flights/domestic/{w['from']}-{w['to']}-{d}"
            f"?adult={w.get('adults', 1)}&fareType=YC")


def fetch_flights(w):
    """SSE 스트림을 끝까지 읽어서 마지막(완료) 이벤트의 항공편 목록을 반환."""
    body = {
        "type": "domestic",
        "device": "pc",
        "fareType": "YC",
        "itineraries": [{
            "departureAirport": w["from"],
            "arrivalAirport": w["to"],
            "departureDate": w["date"].replace("-", ""),
        }],
        "person": {"adult": w.get("adults", 1), "child": 0, "infant": 0},
        "tripType": "OW",
        "initialRequest": True,
        "flightFilter": {"filter": {"type": "departure"}, "limit": 500, "skip": 0,
                         "sort": {"segment.departure.time": 1, "minFare": 1}},
    }
    # 브라우저 헤더가 없으면 nfront 가 503 을 돌려준다
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Origin": "https://flight.naver.com",
        "Referer": page_url(w),
        "User-Agent": UA,
        "sec-ch-ua": '"Chromium";v="129", "Not=A?Brand";v="8"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    })
    last = None
    with urllib.request.urlopen(req, timeout=60) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            last = json.loads(line[5:])
            if last.get("status", {}).get("isComplete"):
                break
    if last is None:
        raise RuntimeError("응답에 데이터가 없음")
    AIRLINE_NAMES.update(last.get("status", {}).get("airlinesCodeMap") or {})
    return last.get("flights") or []


def matches(f, w):
    seg = f["segment"]
    dep = seg["departure"]["time"]
    code = seg["airlineCode"]
    if w.get("airlines") and code not in w["airlines"]:
        return False
    if w.get("flight_numbers"):
        nums = {n.upper().replace(" ", "") for n in w["flight_numbers"]}
        if f"{code}{seg['flightNumber']}" not in nums:
            return False
    if w.get("depart_after") and dep < w["depart_after"].replace(":", ""):
        return False
    if w.get("depart_before") and dep > w["depart_before"].replace(":", ""):
        return False
    if w.get("max_price") and f["minFare"] > w["max_price"]:
        return False
    return True


def describe(f):
    seg = f["segment"]
    code = seg["airlineCode"]
    dep, arr = seg["departure"]["time"], seg["arrival"]["time"]
    seats = f.get("seatCount")
    return (f"{dep[:2]}:{dep[2:]}→{arr[:2]}:{arr[2:]} "
            f"{AIRLINE_NAMES.get(code, code)} {code}{seg['flightNumber']} "
            f"{SEAT_CLASS.get(f.get('seatClass'), f.get('seatClass'))} "
            f"{f['minFare']:,}원" + (f" (잔여 {seats}석)" if seats is not None else ""))


def notify(title, message, url):
    print("\a", end="", flush=True)
    if shutil.which("terminal-notifier"):
        r = subprocess.run(["terminal-notifier", "-title", title, "-message", message,
                            "-open", url, "-sound", "Glass"], capture_output=True)
        if r.returncode == 0:
            return
    script = f'display notification {json.dumps(message, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)} sound name "Glass"'
    subprocess.run(["osascript", "-e", script], capture_output=True)


ALARM_SOUND = "/System/Library/Sounds/Sosumi.aiff"
REOPEN_AFTER = 180  # 같은 페이지를 이 시간(초) 안에 또 열지 않음 - 탭이 계속 쌓이는 것 방지
_popups = {}        # url -> 떠 있는 팝업 프로세스
_opened_at = {}     # url -> 마지막으로 브라우저를 연 시각


def alarm(title, lines, url):
    """배너 알림 + 소리 + 확인 팝업 + 네이버 항공권 페이지 열기."""
    message = "\n".join(lines)
    notify(title, lines[0] + (f" 외 {len(lines) - 1}편" if len(lines) > 1 else ""), url)
    subprocess.Popen(["sh", "-c", f"for i in 1 2 3; do afplay {ALARM_SOUND}; done"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    now = time.time()
    if now - _opened_at.get(url, 0) > REOPEN_AFTER:
        _opened_at[url] = now
        subprocess.run(["open", url])

    # 같은 감시 항목의 이전 팝업이 남아 있으면 최신 내용으로 교체
    old = _popups.pop(url, None)
    if old and old.poll() is None:
        old.terminate()
    script = (f"display alert {json.dumps(title, ensure_ascii=False)} message {json.dumps(message, ensure_ascii=False)} "
              f'buttons {{"닫기", "네이버에서 보기"}} default button 2 giving up after 3600')
    _popups[url] = subprocess.Popen(
        ["sh", "-c", f'r=$(osascript -e {shlex.quote(script)}); '
                     f'case "$r" in *"네이버에서 보기"*) open {shlex.quote(url)};; esac'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def flight_key(f):
    seg = f["segment"]
    return f"{seg['airlineCode']}{seg['flightNumber']}@{seg['departure']['time']}"


def best_by_flight(flights):
    """같은 편이 좌석등급별로 여러 개 올 수 있어 편명 기준 최저가만 남긴다."""
    best = {}
    for f in flights:
        key = flight_key(f)
        if key not in best or f["minFare"] < best[key]["minFare"]:
            best[key] = f
    return best


def check(w, state, first_run):
    """새로 나타났거나 가격이 내려간 항공편 목록을 반환하고 state 를 갱신."""
    best = best_by_flight(f for f in fetch_flights(w) if matches(f, w))
    prev = state.get(w["name"], {})
    changed = [] if first_run else [
        f for k, f in best.items() if k not in prev or f["minFare"] < prev[k]
    ]
    state[w["name"]] = {k: f["minFare"] for k, f in best.items()}
    return sorted(best.values(), key=lambda f: f["segment"]["departure"]["time"]), changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    ap.add_argument("--once", action="store_true", help="한 번만 조회하고 종료")
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    interval = cfg.get("interval_seconds", 60)

    state, first_run = {}, True
    while True:
        now = datetime.now().strftime("%H:%M:%S")
        for w in cfg["watches"]:
            label = f"[{now}] {w['name']} {w['from']}→{w['to']} {w['date']}"
            try:
                current, changed = check(w, state, first_run)
            except Exception as e:  # 네트워크 오류 등은 다음 주기에 재시도
                print(f"{label}: 조회 실패 - {e}", file=sys.stderr)
                continue
            print(f"{label}: 조건 맞는 편 {len(current)}개")
            if first_run:
                for f in current:
                    print(f"    {describe(f)}")
                if current:
                    alarm(f"✈️ {w['name']} {len(current)}편 있음", [describe(f) for f in current], page_url(w))
            for f in changed:
                print(f"    🔔 새로 뜸/가격 하락: {describe(f)}")
            if changed:
                alarm(f"✈️ {w['name']} 새 항공권 {len(changed)}편!", [describe(f) for f in changed], page_url(w))
        first_run = False
        if args.once:
            break
        time.sleep(interval + random.uniform(0, interval * 0.2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
