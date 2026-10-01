#!/usr/bin/env python3
"""국내선 항공권 감시기 (네이버 항공권 + 여기어때).

config.json 의 조건에 맞는 항공편이 새로 생기거나 더 싸지면 macOS 알림을 띄운다.
사용법: python3 watch.py [--once] [--config config.json]
"""
import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import notifier

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
AIRLINE_NAMES = {}  # 응답의 항공사 이름으로 채움
SEAT_CLASS = {"Y": "일반석", "D": "할인석", "L": "특가석", "C": "비즈니스", "F": "일등석"}


# ── 네이버 항공권 ─────────────────────────────────────────────

NAVER_API = "https://flight-api.naver.com/flight/domestic/searchFlights"


def naver_url(w):
    d = w["date"].replace("-", "")
    return (f"https://flight.naver.com/flights/domestic/{w['from']}-{w['to']}-{d}"
            f"?adult={w.get('adults', 1)}&fareType=YC")


def fetch_naver(w):
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
    req = urllib.request.Request(NAVER_API, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Origin": "https://flight.naver.com",
        "Referer": naver_url(w),
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
    return last.get("flights") or [], set()


# ── 여기어때 ──────────────────────────────────────────────────

YEOGI_API = "https://api-gw.goodchoice.kr/flight"
# 여기어때 웹 번들에 들어 있는 고정 클라이언트 헤더 (사용자 인증 정보 아님)
YEOGI_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.yeogi.com",
    "Referer": "https://www.yeogi.com/",
    "User-Agent": UA,
    "x-terminal": "domestic",
    "x-channel": "YEOGI",
    "x-platform": "YEOGI",
    "x-api-token": "19639026a8a517acf1016b9bd42bec82",
    "x-version": "1.0.0",
    "x-device-id": "WEB",
}


def yeogi_url(w):
    q = urllib.parse.urlencode({
        "departureAirportCode": w["from"], "arrivalAirportCode": w["to"],
        "startDate": w["date"].replace("-", ""), "adultCount": w.get("adults", 1),
        "childCount": 0, "infantCount": 0, "seatType": "SALE;NOTY;Y;C", "tripType": "OW",
    })
    return f"https://www.yeogi.com/flight/domestic/srp?{q}"


def _yeogi_get(path, params=None, retries=2):
    url = YEOGI_API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers=YEOGI_HEADERS)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # "스케줄 조회 정보가 없습니다" = 그 항공사 편이 없음
                return {"itineraries": []}
            if attempt == retries:
                raise
            time.sleep(1 + attempt)
        except Exception:
            # 간헐적으로 502 가 나서 잠깐 쉬고 재시도
            if attempt == retries:
                raise
            time.sleep(1 + attempt)


def fetch_yeogi(w):
    """항공사별로 따로 조회해서 네이버 응답과 같은 모양으로 맞춰 반환.

    반환: (항공편 목록, 조회에 실패한 항공사 코드 집합)
    """
    carriers = _yeogi_get("/exhibition/proxy/flight-exhibition/flight/domestic/exhibition/available-carrier/search")
    base = {
        "departureAirportCode": w["from"], "arrivalAirportCode": w["to"],
        "departureDate": w["date"].replace("-", ""),
        "adultCount": w.get("adults", 1), "childCount": 0, "infantCount": 0,
    }
    jobs = []
    for c in carriers.get("carriers", []):
        if c.get("availableYn") != "Y":
            continue
        AIRLINE_NAMES.setdefault(c["carrierCode"], c["carrierName"])
        # 웹에서도 대한항공만 일반석/비즈니스를 나눠서 조회한다
        for cabin in (["Y", "C"] if c["carrierCode"] == "KE" else [None]):
            jobs.append({**base, "carrierCode": c["carrierCode"], **({"cabinClass": cabin} if cabin else {})})

    def get(params):
        try:
            return _yeogi_get("/domestic-avail/proxy/flight-avail/flight/domestic/itineraries", params)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(get, jobs))
    # 여기어때 서버가 특정 항공사만 500 을 내는 경우가 있어 항공사 단위로 실패를 표시한다
    failed = {p["carrierCode"] for p, r in zip(jobs, results) if r is None}
    if jobs and len(failed) == len({p["carrierCode"] for p in jobs}):
        raise RuntimeError("모든 항공사 조회 실패")

    flights = []
    for r in results:
        if r is None:
            continue
        for it in r.get("itineraries", []):
            seats = it.get("seatCount")
            if seats is not None and seats < w.get("adults", 1):
                continue
            adult = it["price"]["adult"]
            fare = adult["saleFare"] + adult.get("taxAmount", 0) + adult.get("fuelCharge", 0) + it["price"].get("tasf", 0)
            dep, arr = it["departureDatetime"], it["arrivalDatetime"]
            flights.append({
                "segment": {
                    "airlineCode": it["carrierCode"], "flightNumber": it["flightNumber"],
                    "departure": {"date": dep[:8], "time": dep[8:12]},
                    "arrival": {"date": arr[:8], "time": arr[8:12]},
                },
                "seatClass": it.get("fareClassName"), "seatCount": seats, "minFare": fare,
            })
    return flights, failed


# ── 공통 ──────────────────────────────────────────────────────

SOURCES = {
    "naver": {"label": "네이버", "fetch": fetch_naver, "url": naver_url},
    "yeogi": {"label": "여기어때", "fetch": fetch_yeogi, "url": yeogi_url},
}


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


def search(w, prev_by_source=None):
    """모든 사이트를 조회해 편명별로 합친다.

    반환: (merged, by_source, errors)
      merged    : key -> 가장 싼 사이트의 항공편 + prices{사이트: 가격} + source
      by_source : 사이트 -> {key: 항공편}  (다음 호출의 prev_by_source 로 넘김)
      errors    : 사이트 -> 오류 메시지 (사이트 전체 실패)
      partial   : 사이트 -> 경고 메시지 (일부 항공사만 실패)
    조회에 실패한 사이트(또는 항공사)는 직전 결과를 그대로 써서, 복구됐을 때 기존 편이 '새로 뜸'으로 울리지 않게 한다.
    """
    prev_by_source = prev_by_source or {}
    by_source, errors, partial = {}, {}, {}

    def run(src):
        flights, failed = SOURCES[src]["fetch"](w)
        best = best_by_flight(f for f in flights if matches(f, w))
        for k, f in prev_by_source.get(src, {}).items():
            if f["segment"]["airlineCode"] in failed:
                best.setdefault(k, f)
        return best, failed

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futures = {src: ex.submit(run, src) for src in SOURCES}
        for src, fut in futures.items():
            try:
                by_source[src], failed = fut.result()
                if failed:
                    partial[src] = "일부 항공사 조회 실패(" + ", ".join(sorted(failed)) + ")"
            except Exception as e:
                errors[src] = str(e)
                by_source[src] = prev_by_source.get(src, {})

    merged = {}
    for src, flights in by_source.items():
        for key, f in flights.items():
            m = merged.setdefault(key, {**f, "prices": {}, "source": src})
            m["prices"][src] = f["minFare"]
            if f["minFare"] < m["minFare"]:
                merged[key] = {**f, "prices": m["prices"], "source": src}
    return merged, by_source, errors, partial


def describe(f):
    seg = f["segment"]
    code = seg["airlineCode"]
    dep, arr = seg["departure"]["time"], seg["arrival"]["time"]
    seats = f.get("seatCount")
    where = SOURCES[f["source"]]["label"] if "source" in f else ""
    return (f"{dep[:2]}:{dep[2:]}→{arr[:2]}:{arr[2:]} "
            f"{AIRLINE_NAMES.get(code, code)} {code}{seg['flightNumber']} "
            f"{SEAT_CLASS.get(f.get('seatClass'), f.get('seatClass'))} "
            f"{f['minFare']:,}원" + (f" (잔여 {seats}석)" if seats is not None else "")
            + (f" · {where}" if where else ""))


REOPEN_AFTER = 180  # 같은 페이지를 이 시간(초) 안에 또 열지 않음 - 탭이 계속 쌓이는 것 방지
_opened_at = {}     # url -> 마지막으로 브라우저를 연 시각


def alarm(title, flights, w):
    """배너 알림 + 소리 + 확인 팝업 + 해당 사이트 항공권 페이지 열기."""
    lines = [describe(f) for f in flights]
    # 알림에 나온 편을 가장 싸게 파는 사이트들의 페이지를 연다
    srcs = list(dict.fromkeys(f["source"] for f in flights))
    urls = {s: SOURCES[s]["url"](w) for s in srcs}

    notifier.banner(title, lines[0] + (f" 외 {len(lines) - 1}편" if len(lines) > 1 else ""), urls[srcs[0]])
    notifier.sound()

    now = time.time()
    for url in urls.values():
        if now - _opened_at.get(url, 0) > REOPEN_AFTER:
            _opened_at[url] = now
            notifier.open_url(url)

    # 팝업 버튼은 최저가 사이트를 맨 앞에 둔다
    order = srcs + [s for s in SOURCES if s not in srcs]
    notifier.popup(f"{w['from']}→{w['to']} {w['date']}", title, "\n".join(lines),
                   [(SOURCES[s]["label"], SOURCES[s]["url"](w)) for s in order])


def main():
    # 윈도우 콘솔(cp949)에서 이모지 출력하다 죽지 않게
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    ap.add_argument("--once", action="store_true", help="한 번만 조회하고 종료")
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    interval = cfg.get("interval_seconds", 60)

    state, first_run = {}, True  # name -> {"by_source": ..., "prices": {key: 최저가}}
    while True:
        now = datetime.now().strftime("%H:%M:%S")
        for w in cfg["watches"]:
            label = f"[{now}] {w['name']} {w['from']}→{w['to']} {w['date']}"
            prev = state.get(w["name"], {})
            merged, by_source, errors, partial = search(w, prev.get("by_source"))
            for src, err in errors.items():
                print(f"{label}: {SOURCES[src]['label']} 조회 실패 - {err}", file=sys.stderr)
            if len(errors) == len(SOURCES) and not prev:
                continue
            prices = prev.get("prices", {})
            changed = [] if first_run else [
                f for k, f in merged.items() if k not in prices or f["minFare"] < prices[k]
            ]
            state[w["name"]] = {"by_source": by_source, "prices": {k: f["minFare"] for k, f in merged.items()}}
            current = sorted(merged.values(), key=lambda f: f["segment"]["departure"]["time"])
            print(f"{label}: 조건 맞는 편 {len(current)}개")
            if first_run:
                for f in current:
                    print(f"    {describe(f)}")
                if current:
                    alarm(f"✈️ {w['name']} {len(current)}편 있음", current, w)
            for f in changed:
                print(f"    🔔 새로 뜸/가격 하락: {describe(f)}")
            if changed:
                alarm(f"✈️ {w['name']} 새 항공권 {len(changed)}편!", changed, w)
        first_run = False
        if args.once:
            break
        time.sleep(interval + random.uniform(0, interval * 0.2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
