
import os
import fcntl
import time
import jsons
import threading
import math
import sys
# Fix PYTHONPATH cho Yocto
site_packages = "/usr/lib/python3.11/site-packages"
if site_packages not in sys.path:
    sys.path.append(site_packages)
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import paho.mqtt.client as mqtt
# ==================== CẤU HÌNH ====================
WIDTH = 320
HEIGHT = 240
FBDEV = "/dev/fb0"
MISCDEV = "/dev/hum_temp"
LIGHTDEV = "/dev/lux_inten"
PUMPLEDDEV = "/dev/pump_led"  # Thêm device cho pump/led driver
BROKER = "192.168.0.101"
PORT = 1883
TOPIC_PUB = "data/sensor/all"
TOPIC_SUB = "control/device/actuator"
# Trạng thái actuator + cảm biến mới nhất
pump_status = "OFF"
led_status = "OFF"
status_lock = threading.Lock()
latest_temp = "?.??"
latest_hum = "?.??"
latest_lux = "?.?? lux"
# ==================== IOCTL ====================
IOC_NONE = 0
IOC_READ = 2
def _IOC(dir_, type_, nr, size):
    return (dir_ << 30) | (ord(type_) << 8) | (nr) | (size << 16)
def _IOR(type_, nr, size):
    return _IOC(IOC_READ, type_, nr, size)
# Ioctl cho sensor (giữ nguyên)
GET_TEMP = _IOR('k', 4, 10)
GET_HUM = _IOR('k', 3, 10)
GET_LUX = _IOR('k', 5, 10)
# Thêm ioctl cho pump/led driver
PUMP_LED_MAGIC = 'p'
def _IO(type_, nr):
    return _IOC(IOC_NONE, type_, nr, 0)  # _IO là no-arg, size=0, dir=NONE
ON_LED = _IO(PUMP_LED_MAGIC, 1)
OFF_LED = _IO(PUMP_LED_MAGIC, 2)
ON_PUMP = _IO(PUMP_LED_MAGIC, 3)
OFF_PUMP = _IO(PUMP_LED_MAGIC, 4)
# ==================== MỞ DEVICE ====================
def open_with_retry(path, flags, retries=10):
    for i in range(retries):
        try:
            fd = os.open(path, flags)
            print(f"Đã mở {path}")
            return fd
        except OSError as e:
            print(f"[{i+1}/{retries}] Chưa mở được {path}: {e}")
            time.sleep(1)
    raise SystemExit(f"Không mở được {path}")
misc_fd = open_with_retry(MISCDEV, os.O_RDONLY)
light_fd = open_with_retry(LIGHTDEV, os.O_RDONLY)
pump_led_fd = open_with_retry(PUMPLEDDEV, os.O_WRONLY)  # Mở WRONLY cho actuator
# ==================== FONT & ICON ====================
try:
    font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 11)
except:
    font_large = font_medium = font_small = ImageFont.load_default()
def draw_tux(draw, x, y, size=20):
    draw.ellipse([x, y, x+size, y+size], fill=(255,255,255))
    draw.ellipse([x+5, y+5, x+size-5, y+size-5], fill=(0,0,0))
def draw_fan(draw, cx, cy, size=30, color=(100,200,255)):
    draw.ellipse([cx-size//2, cy-size//2, cx+size//2, cy+size//2], outline=color, width=3)
    for i in range(0, 360, 90):
        ang = math.radians(i)
        x2 = cx + size//2 * math.cos(ang)
        y2 = cy + size//2 * math.sin(ang)
        draw.line([cx, cy, x2, y2], fill=color, width=3)
def draw_bulb(draw, cx, cy, size=30, is_on=False):
    fill_col = (255,255,100) if is_on else (80,80,80)
    draw.ellipse([cx-size//2, cy-size//2+5, cx+size//2, cy+size//2+5], fill=fill_col, outline=(255,220,0), width=2)
    draw.rectangle([cx-8, cy-size//2+5, cx+8, cy-size//2+15], fill=(150,150,150))
# ==================== ĐIỀU KHIỂN ACTUATOR QUA IOCTL ====================
def control_actuator(device, new_status):
    """Gọi ioctl để điều khiển pump hoặc led"""
    try:
        if device == "PUMP":
            if new_status == "ON":
                fcntl.ioctl(pump_led_fd, ON_PUMP)
            else:
                fcntl.ioctl(pump_led_fd, OFF_PUMP)
        elif device == "LED":
            if new_status == "ON":
                fcntl.ioctl(pump_led_fd, ON_LED)
            else:
                fcntl.ioctl(pump_led_fd, OFF_LED)
        print(f"Điều khiển {device} → {new_status} thành công qua ioctl")
    except Exception as e:
        print(f"Lỗi ioctl {device} → {new_status}: {e}")
        # Có thể revert status nếu lỗi, nhưng ở đây giữ nguyên và log
# ==================== ĐỌC CẢM BIẾN (chỉ 1 lần mỗi 3s) ====================
def read_sensors_once():
    global latest_temp, latest_hum, latest_lux
    for _ in range(3):
        try:
            buf = bytearray(10)
            fcntl.ioctl(misc_fd, GET_TEMP, buf, True)
            temp = buf.split(b'\0',1)[0].decode().strip()
            if not temp or temp == "" or "." not in temp:
                temp = latest_temp
            fcntl.ioctl(misc_fd, GET_HUM, buf, True)
            hum = buf.split(b'\0',1)[0].decode().strip()
            if not hum or hum == "" or hum.startswith("-"):
                hum = latest_hum
            fcntl.ioctl(light_fd, GET_LUX, buf, True)
            lux_raw = buf.split(b'\0',1)[0].decode().strip()
            lux = lux_raw + " lux" if lux_raw else latest_lux
            latest_temp, latest_hum, latest_lux = temp, hum, lux
            return temp, hum, lux
        except Exception as e:
            print(f"ioctl retry... {e}")
            time.sleep(0.3)
    return latest_temp, latest_hum, latest_lux
# ==================== THREAD GỬI MQTT MỖI 3 GIÂY ====================
def sensor_publish_task():
    while True:
        t, h, l = read_sensors_once()
        l_num = l.split()[0] if ' ' in l else l
        try:
            payload = json.dumps({
                "temp": round(float(t), 2),
                "humi": round(float(h), 2),
                "light": round(float(l_num), 2)
            }, separators=(',', ':'))
        except:
            payload = json.dumps({"temp":0.0,"humi":0.0,"light":0.0}, separators=(',', ':'))
        mqtt_client.publish(TOPIC_PUB, payload)
        print(f"Gửi (3s): {payload}")
        time.sleep(3)
threading.Thread(target=sensor_publish_task, daemon=True).start()
# ==================== MQTT CALLBACK ====================
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to MQTT Broker!")
        client.subscribe(TOPIC_SUB)
        print(f"Subscribed to {TOPIC_SUB}")
    else:
        print(f"MQTT connect failed: {rc}")
def on_message(client, userdata, msg):
    global pump_status, led_status
    try:
        payload = msg.payload.decode()
        data = json.loads(payload)
       
        # FIX LỖI: device/state có thể là bool, None, số...
        dev_raw = data.get("device", "")
        state_raw = data.get("state", "")
       
        dev = str(dev_raw).strip().upper() if dev_raw is not None else ""
        state = str(state_raw).strip().upper() if state_raw is not None else ""
       
        print(f"Nhận lệnh: {dev_raw} → {state_raw} (→ {dev} → {state})")
       
        with status_lock:
            updated = False
            if dev in ["PUMP", "BOM", "BƠM"]:
                new_status = "ON" if state in ["ON", "1", "TRUE", "YES"] else "OFF"
                if pump_status != new_status:
                    pump_status = new_status
                    control_actuator("PUMP", new_status)  # Gọi ioctl
                    updated = True
            if dev in ["LED", "DEN", "ĐÈN"]:
                new_status = "ON" if state in ["ON", "1", "TRUE", "YES"] else "OFF"
                if led_status != new_status:
                    led_status = new_status
                    control_actuator("LED", new_status)  # Gọi ioctl
                    updated = True
            if updated:
                print(f"Cập nhật trạng thái: PUMP={pump_status}, LED={led_status}")
    except Exception as e:
        print(f"Lỗi MQTT message: {e} | Raw: {msg.payload}")
mqtt_client = mqtt.Client("SmartFarm_BBB")
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
mqtt_client.connect(BROKER, PORT, 60)
mqtt_client.loop_start()
# ==================== GUI LOOP (500ms) ====================
img = Image.new("RGB", (WIDTH, HEIGHT), (20,20,30))
draw = ImageDraw.Draw(img)
print("SmartFarm GUI + MQTT đang chạy... (GUI 500ms | Sensor 3s)")
try:
    while True:
        temp, hum, lux = latest_temp, latest_hum, latest_lux
        with status_lock:
            p_st = pump_status
            l_st = led_status
        draw.rectangle([0, 0, WIDTH, HEIGHT], fill=(20,20,30))
        # Header
        draw_tux(draw, 10, 3, 24)
        logo_x = (WIDTH - draw.textlength("Smart Farm", font=font_large)) // 2
        draw.text((logo_x, 5), "Smart Farm", fill=(100,255,100), font=font_large)
        draw_tux(draw, WIDTH-34, 3, 24)
        # Sensors
        sy = 32
        sw = (WIDTH - 20) // 3
        draw.rectangle([5, sy, 5+sw, sy+48], fill=(60,20,20), outline=(255,100,100), width=2)
        draw.text((5 + (sw - draw.textlength("TEMP", font=font_small))//2, sy+8), "TEMP", fill=(200,200,200), font=font_small)
        draw.text((5 + (sw - draw.textlength(f"{temp}°C", font=font_medium))//2, sy+24), f"{temp}°C", fill=(255,150,150), font=font_medium)
        draw.rectangle([10+sw, sy, 10+2*sw, sy+48], fill=(20,40,60), outline=(100,200,255), width=2)
        draw.text((10+sw + (sw - draw.textlength("HUM", font=font_small))//2, sy+8), "HUM", fill=(200,200,200), font=font_small)
        draw.text((10+sw + (sw - draw.textlength(f"{hum}%", font=font_medium))//2, sy+24), f"{hum}%", fill=(150,220,255), font=font_medium)
        draw.rectangle([15+2*sw, sy, WIDTH-5, sy+48], fill=(60,50,0), outline=(255,220,0), width=2)
        draw.text((15+2*sw + (sw - draw.textlength("LIGHT", font=font_small))//2, sy+8), "LIGHT", fill=(200,200,200), font=font_small)
        draw.text((15+2*sw + (sw - draw.textlength(lux, font=font_medium))//2, sy+24), lux, fill=(255,240,100), font=font_medium)
        # Actuator panels
        py = sy + 56
        pw = WIDTH // 2 - 8
        iy = py + 47 + 42
        # PUMP
        draw.rectangle([5, py, 5+pw, py+42], fill=(0,60,100), outline=(100,200,255), width=2)
        draw.text((5 + (pw - draw.textlength("PUMP STATUS", font=font_small))//2, py+6), "PUMP STATUS", fill=(180,180,180), font=font_small)
        pc = (100,255,100) if p_st=="ON" else (255,100,100)
        draw.text((5 + (pw - draw.textlength(p_st, font=font_medium))//2, py+22), p_st, fill=pc, font=font_medium)
        # LED
        lx = WIDTH//2 + 3
        draw.rectangle([lx, py, lx+pw, py+42], fill=(80,60,0), outline=(255,220,100), width=2)
        draw.text((lx + (pw - draw.textlength("LED STATUS", font=font_small))//2, py+6), "LED STATUS", fill=(180,180,180), font=font_small)
        lc = (255,255,100) if l_st=="ON" else (255,100,100)
        draw.text((lx + (pw - draw.textlength(l_st, font=font_medium))//2, py+22), l_st, fill=lc, font=font_medium)
        # Icons
        draw_fan(draw, 5 + pw//2, iy + 25, size=22)
        draw_bulb(draw, lx + pw//2, iy + 25, size=22, is_on=(l_st=="ON"))
        # RGB565
        arr = np.array(img)
        r = (arr[...,0] >> 3).astype(np.uint16)
        g = (arr[...,1] >> 2).astype(np.uint16)
        b = (arr[...,2] >> 3).astype(np.uint16)
        rgb565 = (r << 11) | (g << 5) | b
        with open(FBDEV, "wb") as f:
            f.write(rgb565.tobytes())
        print(f"GUI: T={temp}°C H={hum}% L={lux} | PUMP={p_st} LED={l_st}")
        time.sleep(0.5)
except KeyboardInterrupt:
    print("\nDừng chương trình...")
finally:
    mqtt_client.loop_stop()
    mqtt_client.disconnect()
    os.close(misc_fd)
    os.close(light_fd)
    os.close(pump_led_fd) 