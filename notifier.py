"""OS별 알림 기능 (배너 알림, 경고음, 확인 팝업).

macOS, Windows 를 지원하고 그 외 OS 는 가능한 것만 한다. 모두 표준 라이브러리만 쓴다.
"""
import base64
import json
import shlex
import shutil
import subprocess
import sys
import threading
import webbrowser
from xml.sax.saxutils import escape

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"


def open_url(url):
    webbrowser.open(url)


# ── 배너 알림 ─────────────────────────────────────────────────

def banner(title, message, url):
    """화면 구석에 잠깐 뜨는 알림. 누르면 url 이 열린다(가능한 OS 에서만)."""
    try:
        if IS_MAC:
            _banner_mac(title, message, url)
        elif IS_WINDOWS:
            _banner_windows(title, message, url)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", title, message])
    except Exception as e:  # 알림 실패로 감시가 멈추면 안 됨
        print(f"배너 알림 실패: {e}", file=sys.stderr)


def _banner_mac(title, message, url):
    if shutil.which("terminal-notifier"):
        r = subprocess.run(["terminal-notifier", "-title", title, "-message", message,
                            "-open", url, "-sound", "Glass"], capture_output=True)
        if r.returncode == 0:
            return
    script = (f"display notification {json.dumps(message, ensure_ascii=False)} "
              f"with title {json.dumps(title, ensure_ascii=False)} sound name \"Glass\"")
    subprocess.run(["osascript", "-e", script], capture_output=True)


# Windows PowerShell 에 기본으로 등록된 AppID 로 토스트를 띄운다 (별도 설치 불필요)
_PS_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def _banner_windows(title, message, url):
    xml = (f'<toast activationType="protocol" launch="{escape(url, {chr(34): "&quot;"})}">'
           f'<visual><binding template="ToastGeneric">'
           f'<text>{escape(title)}</text><text>{escape(message)}</text>'
           f'</binding></visual></toast>')
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
        "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$doc.LoadXml('{xml.replace(chr(39), chr(39) * 2)}');"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc);"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{_PS_APP_ID}').Show($toast)"
    )
    # 한글/특수문자 따옴표 문제를 피하려고 -EncodedCommand(UTF-16LE base64) 로 넘긴다
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode()
    subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ── 경고음 ────────────────────────────────────────────────────

MAC_SOUND = "/System/Library/Sounds/Sosumi.aiff"


def sound(times=3):
    """알림 설정과 상관없이 직접 경고음을 낸다. 기다리지 않고 바로 반환."""
    if IS_MAC:
        subprocess.Popen(["sh", "-c", f"for i in $(seq {times}); do afplay {MAC_SOUND}; done"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif IS_WINDOWS:
        import winsound

        def play():
            for _ in range(times):
                winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS)
        threading.Thread(target=play, daemon=True).start()
    else:
        print("\a", end="", flush=True)


# ── 확인 팝업 ─────────────────────────────────────────────────

_mac_popups = {}  # key -> 떠 있는 팝업 프로세스


def popup(key, title, message, choices):
    """사용자가 닫을 때까지 떠 있는 팝업. 같은 key 의 이전 팝업은 닫고 새로 띄운다.

    choices: [(버튼 이름, 누르면 열 url), ...] 최대 2개
    """
    try:
        if IS_MAC:
            _popup_mac(key, title, message, choices)
        elif IS_WINDOWS:
            _popup_windows(key, title, message, choices)
    except Exception as e:
        print(f"팝업 실패: {e}", file=sys.stderr)


def _popup_mac(key, title, message, choices):
    old = _mac_popups.pop(key, None)
    if old and old.poll() is None:
        old.terminate()
    buttons = ["닫기"] + [label for label, _ in choices]
    script = (f"display alert {json.dumps(title, ensure_ascii=False)} "
              f"message {json.dumps(message, ensure_ascii=False)} "
              f"buttons {{{', '.join(json.dumps(b, ensure_ascii=False) for b in buttons)}}} "
              f"default button {json.dumps(buttons[1] if choices else buttons[0], ensure_ascii=False)} "
              f"giving up after 3600")
    cases = " ".join(f"*{shlex.quote(label)}*) open {shlex.quote(url)};;" for label, url in choices)
    _mac_popups[key] = subprocess.Popen(
        ["sh", "-c", f'r=$(osascript -e {shlex.quote(script)}); case "$r" in {cases} esac'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# MessageBox 상수
_MB_YESNOCANCEL, _MB_OK = 0x3, 0x0
_MB_ICONINFORMATION, _MB_SETFOREGROUND, _MB_TOPMOST = 0x40, 0x10000, 0x40000
_IDYES, _IDNO, _WM_CLOSE = 6, 7, 0x10


def _popup_windows(key, title, message, choices):
    import ctypes
    user32 = ctypes.windll.user32
    # 창 제목으로 이전 팝업을 찾아 닫는다 - 제목을 항목별로 고정해 둔다
    window_title = f"✈️ 항공권 알림 - {key}"
    hwnd = user32.FindWindowW(None, window_title)
    if hwnd:
        user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)

    # 기본 MessageBox 는 버튼 이름을 바꿀 수 없어서 [예]/[아니요] 에 사이트를 대응시킨다
    choices = choices[:2]
    if choices:
        keys = ["예", "아니요"]
        guide = "   ".join(f"[{keys[i]}] {label}에서 보기" for i, (label, _) in enumerate(choices))
        body = f"{title}\n\n{message}\n\n{guide}   [취소] 닫기"
        flags = _MB_YESNOCANCEL
    else:
        body, flags = f"{title}\n\n{message}", _MB_OK

    def show():
        r = user32.MessageBoxW(None, body, window_title,
                               flags | _MB_ICONINFORMATION | _MB_SETFOREGROUND | _MB_TOPMOST)
        if r == _IDYES and len(choices) >= 1:
            open_url(choices[0][1])
        elif r == _IDNO and len(choices) >= 2:
            open_url(choices[1][1])
    threading.Thread(target=show, daemon=True).start()
