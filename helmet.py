# ============================================================
# HELMET CRACK DETECTION
# Camera: Raspberry Pi NoIR camera (Picamera2)
# Lights: WHITE_LIGHT_PIN (bright light inside/under helmet),
#         IR_LIGHT_PIN (IR illuminator)
# Analysis: Gemini API compares a white-light photo vs an
#           IR-light photo of the same helmet to spot light
#           leaking through a crack.
# ============================================================
#
# GPIO ASSUMPTION: each light is switched on/off through a
# relay or MOSFET driven by a single GPIO pin (gpiozero.LED).
# Change WHITE_LIGHT_PIN / IR_LIGHT_PIN to match your wiring,
# and swap gpiozero.LED for gpiozero.OutputDevice if your
# relay is active-low (active_high=False).
#
# GEMINI API KEY: set it as an environment variable, don't
# hardcode it:
#     export GEMINI_API_KEY="your-key-here"
#
# ============================================================

import os
import time
import threading
import io
import base64

from PIL import Image
from flask import Flask, Response, jsonify, render_template_string

from picamera2 import Picamera2
from gpiozero import LED

import google.generativeai as genai


# ============================================================
# CONFIGURATION
# ============================================================

FRAME_WIDTH = 640
FRAME_HEIGHT = 480
JPEG_QUALITY = 90

WHITE_LIGHT_PIN = 17   # <-- change to your actual GPIO pin
IR_LIGHT_PIN = 27      # <-- change to your actual GPIO pin

LIGHT_SETTLE_SECONDS = 0.5   # time to let a light fully switch + scene stabilize

GEMINI_API_KEY = "put your gemini api key"
GEMINI_MODEL = "gemini-3.6-flash"

CRACK_DETECTION_PROMPT = """
You are inspecting a safety helmet for hairline cracks in its shell.

You are given two photos of the SAME helmet, taken seconds apart from
the same camera position:

1. "White light" photo: a bright light source is placed underneath/inside
   the helmet and turned on. If there is a crack in the shell, light will
   leak through it and appear as a bright spot, line, or glow on the
   helmet's outer surface at the crack location.

2. "IR light" photo: the white light is turned off, an infrared light is
   turned on instead, and the same NoIR (infrared-sensitive) camera takes
   a second photo. This is a comparison/confirmation frame â€” a genuine
   crack should show a corresponding leak or anomaly under IR too,
   while surface scratches, dirt, stickers, paint texture, or reflections
   under white light usually will NOT show the same leak pattern under IR.

Compare the two photos and determine:
- Is there a light leak visible in the white light photo?
- Does a corresponding anomaly appear in the IR photo at the same
  location?
- Based on both, is this most likely an actual crack, or a false
  positive (surface mark, reflection, dirt, decal, lighting artifact)?

Respond in this exact format:

CRACK_DETECTED: yes | no | uncertain
LOCATION: <brief description of where on the helmet, or "none">
CONFIDENCE: low | medium | high
REASONING: <2-3 sentences explaining what you saw in each photo and why>
"""


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

latest_jpeg = None
camera_available = False
picam2 = None
lock = threading.Lock()

white_light = None
ir_light = None

last_result = {
    "status": "idle",
    "crack_detected": None,
    "raw_response": None,
    "white_image_b64": None,
    "ir_image_b64": None,
    "error": None,
}


# ============================================================
# HARDWARE INIT
# ============================================================

def init_camera():
    global picam2, camera_available

    try:
        print("Initializing camera...")

        picam2 = Picamera2()

        config = picam2.create_preview_configuration(
            main={"size": (FRAME_WIDTH, FRAME_HEIGHT), "format": "RGB888"}
        )

        picam2.configure(config)
        picam2.set_controls({"AeEnable": True, "AwbEnable": True})
        picam2.start()
        time.sleep(2)

        camera_available = True
        print("Camera started")

    except Exception as e:
        print("Camera Error:", e)
        camera_available = False


def init_lights():
    global white_light, ir_light

    try:
        white_light = LED(WHITE_LIGHT_PIN)
        ir_light = LED(IR_LIGHT_PIN)

        white_light.off()
        ir_light.off()

        print("Lights initialized (both OFF)")

    except Exception as e:
        print("Light Init Error:", e)


def init_gemini():
    if not GEMINI_API_KEY:
        print("WARNING: GEMINI_API_KEY not set. Analysis will fail until it is.")
        return

    genai.configure(api_key=GEMINI_API_KEY)


# ============================================================
# LIVE CAMERA STREAM (for framing the helmet before capture)
# ============================================================

def camera_loop():
    global latest_jpeg

    while True:
        try:
            if camera_available:
                frame = picam2.capture_array()

                image = Image.fromarray(frame)
                image.thumbnail((FRAME_WIDTH, FRAME_HEIGHT))

                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=JPEG_QUALITY)

                with lock:
                    latest_jpeg = buffer.getvalue()

        except Exception as e:
            print("Camera Loop Error:", e)

        time.sleep(0.05)


def generate_stream():
    while True:
        with lock:
            frame = latest_jpeg

        if frame is not None:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame +
                b"\r\n"
            )

        time.sleep(0.03)


# ============================================================
# CAPTURE HELPERS
# ============================================================

def capture_jpeg_bytes():
    """Grab one fresh frame straight from the camera as JPEG bytes."""
    frame = picam2.capture_array()
    image = Image.fromarray(frame)
    image.thumbnail((FRAME_WIDTH, FRAME_HEIGHT))

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()


def capture_white_and_ir_pair():
    """
    Turns on the white light, captures a photo, turns it off,
    turns on the IR light, captures a second photo, turns it off.
    Returns (white_jpeg_bytes, ir_jpeg_bytes).
    """

    # --- White light photo ---
    ir_light.off()
    white_light.on()
    time.sleep(LIGHT_SETTLE_SECONDS)

    white_jpeg = capture_jpeg_bytes()

    white_light.off()

    # --- IR light photo ---
    ir_light.on()
    time.sleep(LIGHT_SETTLE_SECONDS)

    ir_jpeg = capture_jpeg_bytes()

    ir_light.off()

    return white_jpeg, ir_jpeg


# ============================================================
# GEMINI ANALYSIS
# ============================================================

def analyze_with_gemini(white_jpeg, ir_jpeg):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set")

    model = genai.GenerativeModel(GEMINI_MODEL)

    white_part = {"mime_type": "image/jpeg", "data": white_jpeg}
    ir_part = {"mime_type": "image/jpeg", "data": ir_jpeg}

    response = model.generate_content([
        CRACK_DETECTION_PROMPT,
        "White light photo:",
        white_part,
        "IR light photo:",
        ir_part,
    ])

    return response.text


def run_capture_and_analyze():
    global last_result

    with lock:
        last_result["status"] = "capturing"
        last_result["error"] = None

    try:
        white_jpeg, ir_jpeg = capture_white_and_ir_pair()

        with lock:
            last_result["status"] = "analyzing"
            last_result["white_image_b64"] = base64.b64encode(white_jpeg).decode()
            last_result["ir_image_b64"] = base64.b64encode(ir_jpeg).decode()

        raw_response = analyze_with_gemini(white_jpeg, ir_jpeg)

        crack_detected = None
        for line in raw_response.splitlines():
            if line.strip().upper().startswith("CRACK_DETECTED:"):
                crack_detected = line.split(":", 1)[1].strip()
                break

        with lock:
            last_result["status"] = "done"
            last_result["crack_detected"] = crack_detected
            last_result["raw_response"] = raw_response

    except Exception as e:
        print("Capture/Analyze Error:", e)
        with lock:
            last_result["status"] = "error"
            last_result["error"] = str(e)


# ============================================================
# WEB INTERFACE
# ============================================================

HTML = """
<!DOCTYPE html>
<html>
<head>
<title>Helmet Crack Detection</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; font-family: Arial, sans-serif; }
  body { margin:0; background:#0b0f14; color:white; }
  .header { padding:20px; background:#111720; border-bottom:1px solid #263241; }
  .container { max-width:900px; margin:auto; padding:15px; }
  .card { background:#151d27; border:1px solid #263241; border-radius:12px; padding:15px; margin-bottom:15px; }
  img.live { width:100%; border-radius:10px; background:black; }
  img.result { width:48%; border-radius:8px; }
  button { padding:14px 20px; background:#111720; border:1px solid #415166; color:white;
           border-radius:7px; font-weight:bold; cursor:pointer; font-size:16px; width:100%; }
  button:hover { background:#1c2733; }
  .yes { color:#ef5350; font-weight:bold; }
  .no { color:#43d17a; font-weight:bold; }
  .uncertain { color:#ffb74d; font-weight:bold; }
  pre { white-space: pre-wrap; color:#9ca8b5; font-size:13px; }
</style>
</head>
<body>

<div class="header"><h1 style="margin:0">Helmet Crack Detection</h1></div>

<div class="container">

  <div class="card">
    <div style="margin-bottom:10px;font-weight:bold">LIVE PREVIEW</div>
    <img class="live" src="/video_feed">
  </div>

  <div class="card">
    <button onclick="runCheck()">Capture &amp; Analyze</button>
    <div id="status" style="margin-top:10px;color:#9ca8b5"></div>
  </div>

  <div class="card" id="resultCard" style="display:none">
    <div style="margin-bottom:10px;font-weight:bold">RESULT</div>
    <div id="verdict"></div>
    <pre id="reasoning"></pre>
    <div style="margin-top:10px">
      <img class="result" id="whiteImg">
      <img class="result" id="irImg">
    </div>
  </div>

</div>

<script>
function runCheck() {
    document.getElementById("status").innerText = "Capturing and analyzing...";
    document.getElementById("resultCard").style.display = "none";

    fetch("/capture_and_analyze", { method: "POST" })
    .then(r => r.json())
    .then(poll)
    .catch(e => document.getElementById("status").innerText = "Error: " + e);
}

function poll() {
    fetch("/result")
    .then(r => r.json())
    .then(data => {

        document.getElementById("status").innerText = "Status: " + data.status;

        if (data.status === "capturing" || data.status === "analyzing") {
            setTimeout(poll, 800);
            return;
        }

        if (data.status === "error") {
            document.getElementById("status").innerText = "Error: " + data.error;
            return;
        }

        if (data.status === "done") {
            document.getElementById("resultCard").style.display = "block";

            let cls = "uncertain";
            if (data.crack_detected === "yes") cls = "yes";
            if (data.crack_detected === "no") cls = "no";

            document.getElementById("verdict").innerHTML =
                "Crack detected: <span class='" + cls + "'>" + data.crack_detected + "</span>";

            document.getElementById("reasoning").innerText = data.raw_response;

            document.getElementById("whiteImg").src = "data:image/jpeg;base64," + data.white_image_b64;
            document.getElementById("irImg").src = "data:image/jpeg;base64," + data.ir_image_b64;
        }
    });
}
</script>

</body>
</html>
"""


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def home():
    return render_template_string(HTML)


@app.route("/video_feed")
def video_feed():
    return Response(
        generate_stream(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}
    )


@app.route("/capture_and_analyze", methods=["POST"])
def capture_and_analyze():
    # Run in a background thread so the request returns immediately
    # and the page can poll /result for progress.
    thread = threading.Thread(target=run_capture_and_analyze, daemon=True)
    thread.start()
    return jsonify({"started": True})


@app.route("/result")
def result():
    with lock:
        return jsonify(dict(last_result))


# ============================================================
# CLEANUP
# ============================================================

def cleanup():
    print("\nShutting down...")
    try:
        if white_light is not None:
            white_light.off()
        if ir_light is not None:
            ir_light.off()
    except Exception:
        pass

    try:
        if picam2 is not None:
            picam2.stop()
    except Exception:
        pass


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    try:
        init_lights()
        init_camera()
        init_gemini()

        cam_thread = threading.Thread(target=camera_loop, daemon=True)
        cam_thread.start()

        print("====================================")
        print("Helmet crack detection server running")
        print("Open: http://127.0.0.1:5000")
        print("Or from another device: http://<pi-ip>:5000")
        print("====================================")

        app.run(host="0.0.0.0", port=5000, threaded=True, debug=False, use_reloader=False)

    except KeyboardInterrupt:
        print("\nCTRL+C detected.")

    finally:
        cleanup()
