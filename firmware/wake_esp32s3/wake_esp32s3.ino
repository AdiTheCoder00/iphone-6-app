/* ===========================================================================
 * Companion wake trigger — ESP32-S3
 * ---------------------------------------------------------------------------
 * Listens on an I2S microphone and POSTs to the companion backend when it
 * decides someone is talking to it:
 *
 *     POST http://<BACKEND_HOST>:<BACKEND_PORT>/wake
 *     { "source": "esp32", "timestamp": <millis since boot> }
 *
 * The backend debounces (3s) and pushes a "wake" event down its SSE stream;
 * the phone reacts by listening and auto-recording. Nothing else on the
 * device matters — this board's entire job is to decide "now".
 *
 * ---------------------------------------------------------------------------
 * DETECTOR: adaptive energy threshold, NOT a real wake word.
 *
 * This is the simplest thing that works, and the tradeoff is real and worth
 * understanding before you rely on it:
 *
 *   What you get   Zero training, ~200 lines, runs in a few KB of RAM, and
 *                  adapts to the room's noise floor on its own.
 *   What it costs  It fires on ANY sustained sound above the floor — speech
 *                  to someone else, a TV, a door, a laugh. It is really a
 *                  "speak near me to wake" trigger, not a keyword.
 *
 * That is usually fine on a quiet desk and unusable in a busy room. When it
 * gets annoying, swap in a trained keyword model: only detectWake() below
 * has to change. Export an Edge Impulse "keyword spotting" model as an
 * Arduino library, then call run_classifier() on the same sample buffer and
 * return true when your keyword's score clears ~0.8. Everything else here —
 * I2S, WiFi, debounce, POST — stays exactly as it is.
 *
 * ---------------------------------------------------------------------------
 * BUILD
 *   Arduino IDE with esp32 board package 3.x (the ESP_I2S.h API below is 3.x;
 *   2.x used driver/i2s.h and will not compile).
 *   Board: "XIAO_ESP32S3" or "ESP32S3 Dev Module" to match BOARD_* below.
 *   Tools > USB CDC On Boot > Enabled, so Serial shows up over USB.
 *
 * TUNING
 *   Open Serial Monitor at 115200. Every block prints rms and the current
 *   noise floor. Speak at a normal volume from where you actually sit and
 *   watch the ratio; set TRIGGER_RATIO a little below what speech reaches.
 * ======================================================================== */

#include <WiFi.h>
#include <HTTPClient.h>
#include <ESP_I2S.h>

/* --- board selection ------------------------------------------------------
 * Exactly one of these. XIAO is the default: its mic is on the board, so
 * there is no wiring to get wrong.
 */
#define BOARD_XIAO_ESP32S3_SENSE
// #define BOARD_DEVKITC_INMP441

/* --- your network and backend ---------------------------------------------
 * Machine-specific values live in config.h (WiFi, backend address, token).
 * It is gitignored so the backend token never enters the repository. Copy
 * config.h.example to config.h and fill it in; this file refuses to compile
 * until that is done, so a half-configured board cannot be flashed silently.
 */
#include "config.h"

#ifndef COMPANION_CONFIG_PROVIDED
  #error "Missing wake_esp32s3/config.h — copy config.h.example to config.h and set WIFI_SSID, WIFI_PASSWORD, BACKEND_HOST and BACKEND_TOKEN."
#endif

/* --- audio ---------------------------------------------------------------- */
static const uint32_t SAMPLE_RATE = 16000;
/* 512 samples = 32ms at 16kHz. Small enough to react quickly, large enough
 * that one RMS figure is a stable measure of the block. */
static const size_t SAMPLES_PER_BLOCK = 512;

/* --- detector tuning ------------------------------------------------------
 * Speech must be TRIGGER_RATIO times the running noise floor for
 * TRIGGER_BLOCKS consecutive blocks. The ratio rejects steady background
 * noise (fans, traffic); the block count rejects transients (a door, a cough,
 * a keyboard) that are loud but over in one block.
 */
static const float  TRIGGER_RATIO  = 3.5f;
static const int    TRIGGER_BLOCKS = 6;      // 6 * 32ms ≈ 190ms of sound
/* Absolute floor, so a silent room's noise floor near zero cannot make any
 * tiny sound look like a 3.5x spike. Raise if it self-triggers in silence. */
static const float  MIN_RMS = 900.0f;
/* How fast the noise floor tracks the room. Deliberately slow: it must follow
 * the air conditioning coming on, not the speech we are trying to detect. */
static const float  NOISE_ADAPT = 0.02f;
/* Local cooldown. The backend also debounces at 3s; this just avoids sending
 * requests we know will be discarded. */
static const uint32_t COOLDOWN_MS = 4000;

/* Set false once tuned — printing every block is itself a small load. */
static const bool DEBUG_LEVELS = true;

/* ========================================================================= */

I2SClass I2S;

static int16_t   sampleBuffer[SAMPLES_PER_BLOCK];
static float     noiseFloor   = 0.0f;
static int       loudBlocks   = 0;
static uint32_t  lastWakeMs   = 0;
static bool      noiseFloorPrimed = false;

/* --- setup ---------------------------------------------------------------- */

/* Non-blocking WiFi: starts a connect attempt on the first call, then just
 * polls status on later calls. Never delays — the audio loop keeps reading
 * I2S throughout an outage, so the detector stays live even with no network.
 * A failed attempt is retried on the next loop() pass. */
static uint32_t wifiAttemptStart = 0;
static bool     wifiAttempting   = false;

static void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    if (wifiAttempting) {
      wifiAttempting = false;
      Serial.print("[wifi] connected, ip=");
      Serial.println(WiFi.localIP());
    }
    return;
  }

  if (!wifiAttempting) {
    wifiAttempting = true;
    wifiAttemptStart = millis();
    WiFi.mode(WIFI_STA);
    /* Sleep would save power but adds latency to every POST and can drop the
     * association on some APs. This board is mains-powered on a desk. */
    WiFi.setSleep(false);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.printf("[wifi] connecting to %s\n", WIFI_SSID);
    return;
  }

  if (millis() - wifiAttemptStart > 20000) {
    wifiAttempting = false;
    Serial.println("[wifi] FAILED — will retry in loop()");
  }
}

static bool startMicrophone() {
#if defined(BOARD_XIAO_ESP32S3_SENSE)
  /* Seeed XIAO ESP32S3 Sense: onboard PDM mic on fixed pins.
   *   GPIO42 = PDM clock, GPIO41 = PDM data. No wiring required. */
  I2S.setPinsPdmRx(42, 41);
  if (!I2S.begin(I2S_MODE_PDM_RX, SAMPLE_RATE,
                 I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    Serial.println("[i2s] PDM begin failed");
    return false;
  }
  Serial.println("[i2s] XIAO ESP32S3 Sense, onboard PDM mic");

#elif defined(BOARD_DEVKITC_INMP441)
  /* ESP32-S3-DevKitC-1 + INMP441 breakout. Any free GPIOs work; these avoid
   * the strapping and USB pins. Wire it as:
   *
   *     INMP441        ESP32-S3
   *     -------        --------
   *     VDD    ------> 3V3      (NOT 5V)
   *     GND    ------> GND
   *     SCK    ------> GPIO4    (bit clock)
   *     WS     ------> GPIO5    (word select / LR clock)
   *     SD     ------> GPIO6    (data out of mic, into the ESP32)
   *     L/R    ------> GND      (selects the left slot; must not float)
   */
  const int PIN_BCLK = 4;
  const int PIN_WS   = 5;
  const int PIN_DIN  = 6;
  /* setPins(bclk, ws, dout, din, mclk) — dout/mclk unused for capture. */
  I2S.setPins(PIN_BCLK, PIN_WS, -1, PIN_DIN, -1);
  if (!I2S.begin(I2S_MODE_STD, SAMPLE_RATE,
                 I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    Serial.println("[i2s] STD begin failed — check wiring and L/R to GND");
    return false;
  }
  Serial.println("[i2s] DevKitC + INMP441 on GPIO4/5/6");

#else
  #error "Define BOARD_XIAO_ESP32S3_SENSE or BOARD_DEVKITC_INMP441"
#endif

  return true;
}

void setup() {
  Serial.begin(115200);
  delay(400);                       // let USB CDC enumerate before printing
  Serial.println("\n[boot] companion wake trigger");

  if (strcmp(BACKEND_TOKEN, "PASTE_COMPANION_TOKEN_HERE") == 0) {
    Serial.println("[config] BACKEND_TOKEN is still the placeholder — set your real token in config.h");
    while (true) delay(1000);       // fail fast, visibly, instead of flashing a 401ing board
  }

  ensureWiFi();                     // kicks off the attempt, returns immediately
  if (!startMicrophone()) {
    Serial.println("[boot] microphone unavailable — halted");
    while (true) delay(1000);
  }
}

/* --- detector -------------------------------------------------------------
 * Returns true exactly once per utterance: on the block where the sustained
 * loudness requirement is first met.
 */

static float blockRms(const int16_t* samples, size_t count) {
  /* double accumulator: 512 squared int16s overflow a float's precision
   * budget quickly enough to skew the result. */
  double sum = 0.0, mean = 0.0;
  /* AC RMS: subtract the block mean first. MEMS/PDM mics carry a large DC
   * bias (INMP441 especially), and without removal the floor is dominated by
   * it, corrupting the noise-floor threshold. */
  for (size_t i = 0; i < count; i++) mean += (double)samples[i];
  mean /= (double)count;
  for (size_t i = 0; i < count; i++) {
    double s = (double)samples[i] - mean;
    sum += s * s;
  }
  return (float)sqrt(sum / (double)count);
}

static bool detectWake(const int16_t* samples, size_t count) {
  const float rms = blockRms(samples, count);

  /* First block after boot: seed the floor rather than treating the whole
   * initial level as a spike. */
  if (!noiseFloorPrimed) {
    noiseFloor = rms;
    noiseFloorPrimed = true;
    return false;
  }

  const float threshold = fmaxf(noiseFloor * TRIGGER_RATIO, MIN_RMS);
  const bool loud = rms > threshold;

  if (DEBUG_LEVELS) {
    Serial.printf("[lvl] rms=%7.0f floor=%7.0f thr=%7.0f %s\n",
                  rms, noiseFloor, threshold, loud ? "LOUD" : "");
  }

  if (loud) {
    loudBlocks++;
    /* Do NOT fold speech into the noise floor — that is what makes the
     * detector go deaf to a long sentence. */
  } else {
    loudBlocks = 0;
    noiseFloor = (1.0f - NOISE_ADAPT) * noiseFloor + NOISE_ADAPT * rms;
  }

  if (loudBlocks >= TRIGGER_BLOCKS) {
    loudBlocks = 0;               // one trigger per utterance
    return true;
  }
  return false;
}

/* --- backend -------------------------------------------------------------- */

static void postWake() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[wake] skipped — no wifi");
    return;
  }

  char url[96];
  snprintf(url, sizeof(url), "http://%s:%u/wake", BACKEND_HOST, BACKEND_PORT);

  char body[96];
  snprintf(body, sizeof(body),
           "{\"source\":\"esp32\",\"timestamp\":%lu}", (unsigned long)millis());

  HTTPClient http;
  http.setConnectTimeout(2000);
  http.setTimeout(3000);          // never let a stalled backend block listening

  if (!http.begin(url)) {
    Serial.println("[wake] http.begin failed");
    return;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Companion-Token", BACKEND_TOKEN);

  const int status = http.POST((uint8_t*)body, strlen(body));
  if (status > 0) {
    Serial.printf("[wake] POST %s -> %d %s\n", url, status, http.getString().c_str());
  } else {
    Serial.printf("[wake] POST failed: %s\n", HTTPClient::errorToString(status).c_str());
  }
  http.end();
}

/* --- loop ----------------------------------------------------------------- */

void loop() {
  /* Reconnect opportunistically, never blocking: ensureWiFi() starts an
   * attempt and returns; the detector keeps listening either way, so a WiFi
   * outage costs triggers but never wedges the board. */
  ensureWiFi();

  const size_t wanted = SAMPLES_PER_BLOCK * sizeof(int16_t);
  const size_t got = I2S.readBytes((char*)sampleBuffer, wanted);
  if (got < wanted) {
    /* Short read: nothing buffered yet. Yield rather than spin. */
    delay(2);
    return;
  }

  if (!detectWake(sampleBuffer, SAMPLES_PER_BLOCK)) return;

  const uint32_t now = millis();
  if (now - lastWakeMs < COOLDOWN_MS) {
    Serial.println("[wake] detected but within cooldown");
    return;
  }
  lastWakeMs = now;

  Serial.println("[wake] TRIGGER");
  postWake();
}
