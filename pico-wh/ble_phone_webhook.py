import bluetooth
import network
import time
import struct
import gc
from micropython import const
from machine import Pin

try:
    import urequests as requests
except ImportError:
    import requests

try:
    import secrets
except ImportError:
    raise ImportError(
        "secrets.py がありません。secrets.example.py をコピーして設定してください"
    )

WIFI_SSID = secrets.WIFI_SSID
WIFI_PASSWORD = secrets.WIFI_PASSWORD
WEBHOOK_URL = secrets.WEBHOOK_URL
WEBHOOK_STYLE = secrets.WEBHOOK_STYLE
WEBHOOK_API_KEY = secrets.WEBHOOK_API_KEY

_IRQ_SCAN_RESULT = const(5)
_IRQ_SCAN_DONE = const(6)

TARGET_UUID16 = const(0xFFF0)
TIMEOUT_MS = 3000
WIFI_TIMEOUT_MS = 20000
WEBHOOK_RETRY = 2
LED_PULSE_MS = 80
LED_PRESENT_INTERVAL_MS = 4000
LED_ABSENT_INTERVAL_MS = 800

led = Pin("LED", Pin.OUT)
led.off()

last_seen = 0
last_rssi = 0
scan_done = True
present = None  # None=起動直後, True=検知中, False=不在
led_cycle_started = 0
led_was_present = None


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        print("Wi-Fi接続済み:", wlan.ifconfig()[0])
        return wlan

    print("Wi-Fi接続中...")
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    started = time.ticks_ms()
    while not wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), started) > WIFI_TIMEOUT_MS:
            raise OSError("Wi-Fi接続タイムアウト")
        time.sleep_ms(200)

    print("Wi-Fi接続:", wlan.ifconfig()[0])
    return wlan


def uuid16_of(target):
    if isinstance(target, int):
        return target
    return 0xFFF0


def has_uuid(adv_data, target):
    target16 = uuid16_of(target)
    i = 0
    n = len(adv_data)
    while i < n:
        length = adv_data[i]
        if length == 0:
            break
        if i + 1 + length > n:
            break

        type_ = adv_data[i + 1]
        data = adv_data[i + 2 : i + 1 + length]

        # 0x02: Incomplete / 0x03: Complete 16-bit Service UUIDs
        if type_ in (0x02, 0x03):
            for j in range(0, len(data) - 1, 2):
                uuid16 = struct.unpack("<H", data[j : j + 2])[0]
                if uuid16 == target16:
                    return True

        # 0x06 / 0x07: 128-bit Service UUIDs（0000fff0-0000-1000-8000-00805f9b34fb）
        if type_ in (0x06, 0x07):
            for j in range(0, len(data) - 15, 16):
                uuid16 = struct.unpack("<H", data[j + 12 : j + 14])[0]
                if uuid16 == target16:
                    return True

        i += 1 + length
    return False


def format_checked_at():
    t = time.localtime()
    return "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}+09:00".format(
        t[0], t[1], t[2], t[3], t[4], t[5]
    )


def build_payload(is_present, rssi):
    text = "スマホを検知しました" if is_present else "スマホが離れました"
    if rssi:
        text += " (RSSI: {})".format(rssi)

    if WEBHOOK_STYLE == "discord":
        return {"content": text}
    if WEBHOOK_STYLE == "slack":
        return {"text": text}
    if WEBHOOK_STYLE == "ntfy":
        return text
    return {
        "event": "connected" if is_present else "disconnected",
        "online": bool(is_present),
        "checked_at": format_checked_at(),
    }


def send_webhook(is_present, rssi):
    if not WEBHOOK_URL or "xxxx" in WEBHOOK_URL:
        print("WEBHOOK_URL が未設定です")
        return False

    payload = build_payload(is_present, rssi)
    headers = {"Content-Type": "application/json"}
    if WEBHOOK_API_KEY:
        headers["x-api-key"] = WEBHOOK_API_KEY
    last_error = None

    for attempt in range(1, WEBHOOK_RETRY + 1):
        gc.collect()
        resp = None
        try:
            if WEBHOOK_STYLE == "ntfy":
                resp = requests.post(WEBHOOK_URL, data=payload)
            else:
                resp = requests.post(WEBHOOK_URL, json=payload, headers=headers)

            ok = 200 <= resp.status_code < 300
            print("Webhook {}: {} {}".format(
                "成功" if ok else "失敗",
                resp.status_code,
                resp.text[:80] if resp.text else "",
            ))
            resp.close()
            return ok
        except Exception as e:
            last_error = e
            print("Webhook例外 ({}/{}): {}".format(attempt, WEBHOOK_RETRY, e))
            if resp:
                try:
                    resp.close()
                except Exception:
                    pass
            time.sleep_ms(500)

    print("Webhook送信失敗:", last_error)
    return False


def bt_irq(event, data):
    global last_seen, last_rssi, scan_done
    if event == _IRQ_SCAN_RESULT:
        addr_type, addr, adv_type, rssi, adv_data = data
        if has_uuid(adv_data, TARGET_UUID16):
            last_seen = time.ticks_ms()
            last_rssi = rssi
    elif event == _IRQ_SCAN_DONE:
        scan_done = True


def update_led(is_present):
    global led_cycle_started, led_was_present
    now = time.ticks_ms()
    if is_present != led_was_present:
        led_cycle_started = now
        led_was_present = is_present

    interval = LED_PRESENT_INTERVAL_MS if is_present else LED_ABSENT_INTERVAL_MS
    elapsed = time.ticks_diff(now, led_cycle_started)
    if elapsed >= interval:
        led_cycle_started = now
        elapsed = 0

    if elapsed < LED_PULSE_MS:
        led.on()
    else:
        led.off()


wlan = connect_wifi()

ble = bluetooth.BLE()
ble.active(True)
ble.irq(bt_irq)

print("監視開始: UUID 0xFFF0")
print("状態変化時だけ Webhook 通知します")

while True:
    if scan_done:
        scan_done = False
        # 2秒スキャン。Wi-Fiと電波を共有するので連続スキャンは避ける
        ble.gap_scan(2000, 30000, 30000, True)

    seen_recently = (
        last_seen != 0
        and time.ticks_diff(time.ticks_ms(), last_seen) <= TIMEOUT_MS
    )

    if present is None:
        if seen_recently:
            present = True
            print("起動時検出 RSSI: {}（通知なし）".format(last_rssi))
        elif time.ticks_ms() > TIMEOUT_MS:
            present = False
            print("起動後タイムアウト: 未検出（通知なし）")
    elif seen_recently != present:
        present = seen_recently
        if present:
            print("状態変化: 出現 RSSI:", last_rssi)
            send_webhook(True, last_rssi)
        else:
            print("状態変化: 離脱")
            send_webhook(False, last_rssi)

    update_led(present is True)

    if not wlan.isconnected():
        print("Wi-Fi切断。再接続します")
        try:
            connect_wifi()
        except Exception as e:
            print("Wi-Fi再接続失敗:", e)

    time.sleep_ms(100)
