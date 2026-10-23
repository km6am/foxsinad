/* foxsinad_fw - serial-controlled RF test source for the PicoFox r3

Fork of PicoFox-Firmware (Copyright 2025 Giorgi Enterprises LLC dba
AI6YM.radio, https://github.com/ai6ym/PicoFox), reusing its SI5351
modulation scheme, pin map, and BVA1761 attenuator driver.
License: CC BY-NC-SA 4.0, same as upstream (see ../LICENSE).

Unlike the stock fox firmware this build is STATELESS: no flash
filesystem, no USB mass storage, no settings.txt. Every parameter
arrives over USB CDC serial, one command per line (see ../PROTOCOL.md).
RF cannot be enabled until a callsign is set (CALL), and a morse ID is
keyed automatically while transmitting (Part 97.119).

Core split: core 0 parses serial and fills a single-slot mailbox;
core 1 owns ALL RF hardware (SI5351 over I2C, and the attenuator, which
bit-bangs the SAME SDA/SCL pins and therefore must never run while the
other core is mid-I2C-transaction).

Board: Raspberry Pi Pico (RP2040), Arduino-Pico core, default Pico SDK
USB stack (no Adafruit TinyUSB needed -- there is no MSC device).
Library: Etherkit Si5351.
*/

#include <Arduino.h>
#include <Wire.h>
#include <si5351.h>

#define FW_VERSION "0.1.0"

// ---- pin map and RF constants (verbatim from upstream) ----
#define ID0 22u
#define ID1 23u
#define ID2 24u
#define ID3 25u
#define SDA_PIN 0u
#define SCL_PIN 1u
// Upstream builds against AI6YM's arduino-pico fork whose "ai6ym_picofox"
// variant maps Wire0 to GPIO 0/1 and the LED to GPIO 3. We build on
// mainline Arduino-Pico (plain rpipico variant: Wire0 on 4/5, LED on 25),
// so both must be set explicitly or I2C lands on the wrong pins -- GPIO 4
// is the attenuator latch -- and the Si5351 never hears a write.
#define LED_PIN 3u
#define I2C_CLOCK_HZ 1000000  // >= 800 kHz needed for per-sample PLL writes.
#define AMP_EN 2u             // LOW enables the RF amplifier.
#define ATTN_LE 4u            // Latch pin of the BVA1761 attenuator.
#define AUDIO_SAMPLE_RATE_HZ 5000
#define SI5351_PLL SI5351_PLLA
#define SI5351_CLOCK_OUTPUT SI5351_CLK0
#define SI5351_CLOCK_DIV 6
#define SI5351_DRIVE_LEVEL SI5351_DRIVE_2MA  // Higher adds little output but adds spurs.
#define SAMPLE_WRITE_CORR_US -3
#define MAX_UINT32 4294967295UL

#define MIN_FREQ_MHZ 144.0
#define MAX_FREQ_MHZ 148.0
#define MAX_DEVIATION_HZ 10000
#define FREQ_MARGIN_HZ (2 * MAX_DEVIATION_HZ)

// Morse ID parameters (fixed; the ID is a legal formality, not a measurement).
#define ID_TONE_HZ 600
#define ID_WPM 20
#define ID_INTERVAL_MS 540000UL  // 9 min: inside the 10-min Part 97.119 window.

// ---- shared state: single writer per field ----
// Mailbox, written by core 0 then cmdSeq incremented; core 1 copies the
// command out when cmdSeq != ackSeq and increments ackSeq when applied.
enum RfMode : uint8_t { RF_OFF = 0, RF_CARRIER, RF_TONE };

struct Command {
  RfMode mode;
  double freqMHz;       // carrier
  uint16_t toneHz;      // TONE only
  uint16_t devHz;       // TONE only
  uint8_t attenuation;  // 0-127 x 0.25 dB
  bool setFreq, setAtt, setMode, sendId;
};
volatile Command mailbox;
volatile uint32_t cmdSeq = 0;
volatile uint32_t ackSeq = 0;

// Live state, written only by core 1; core 0 reads it for STATUS.
volatile RfMode curMode = RF_OFF;
volatile double curFreqMHz = 146.565;
volatile uint16_t curToneHz = 1000;
volatile uint16_t curDevHz = 3000;
volatile uint8_t curAtt = 127;  // boot at max attenuation: least surprise
volatile bool idActive = false;
volatile uint32_t lastIdMillis = 0;
volatile bool txSinceId = false;

char callsign[12] = "";  // written by core 0 before any RF is allowed on
uint16_t revision = 0;

Si5351 si5351;
bool wireRunning = false;

// 256-entry quarter-resolution sine table is ~-50 dB THD, well below
// every other distortion source in this chain.
int16_t sineTable[256];

// ---- I2C / attenuator (from upstream: shared pins, never concurrent) ----
void startWire() {
  if (wireRunning) return;
  Wire.setSDA(SDA_PIN);  // mainline variant defaults to 4/5; see LED_PIN note
  Wire.setSCL(SCL_PIN);
  Wire.begin();
  Wire.setClock(I2C_CLOCK_HZ);
  wireRunning = true;
}

void stopWire() {
  if (!wireRunning) return;
  Wire.end();
  wireRunning = false;
}

// Programs the BVA1761 attenuator (r3 only). Reclaims the shared SDA/SCL
// pins, so this must only ever run on core 1 between samples.
void programAttenuator(uint8_t attenuation) {
  if (revision != 3) return;
  if (attenuation > 127) attenuation = 127;

  stopWire();
  pinMode(ATTN_LE, OUTPUT);
  digitalWrite(ATTN_LE, LOW);
  // Hold SCL low before touching SDA so the shared bus never sees an I2C START.
  pinMode(SCL_PIN, OUTPUT);
  digitalWrite(SCL_PIN, LOW);
  pinMode(SDA_PIN, OUTPUT);
  digitalWrite(SDA_PIN, LOW);

  // Attenuation byte LSB first, then 8 zero address bits.
  uint8_t mask = 1;
  for (int i = 0; i < 16; i++) {
    if (i < 8) {
      if ((attenuation & mask) > 0) {
        pinMode(SDA_PIN, INPUT);
      } else {
        pinMode(SDA_PIN, OUTPUT);
        digitalWrite(SDA_PIN, LOW);
      }
      mask = mask << 1;
    } else {
      pinMode(SDA_PIN, OUTPUT);
      digitalWrite(SDA_PIN, LOW);
    }
    pinMode(SCL_PIN, INPUT);
    delayMicroseconds(1);
    pinMode(SCL_PIN, OUTPUT);
    digitalWrite(SCL_PIN, LOW);
    delayMicroseconds(1);
  }
  digitalWrite(ATTN_LE, HIGH);
  delayMicroseconds(1);
  digitalWrite(ATTN_LE, LOW);

  startWire();
}

// ---- SI5351 (from upstream: fixed /6 multisynth, modulation on the PLL) ----
void setRf(bool enabled) {
  si5351.output_enable(SI5351_CLOCK_OUTPUT, enabled ? 1 : 0);
  digitalWrite(AMP_EN, enabled ? LOW : HIGH);
  digitalWrite(LED_PIN, enabled ? HIGH : LOW);
}

void tuneCarrier(double freqMHz) {
  uint64_t freq = (uint64_t)(freqMHz * 1e8);  // 0.01 Hz units
  si5351.set_pll(freq * SI5351_CLOCK_DIV, SI5351_PLL);
  si5351.set_freq_manual(freq, si5351.plla_freq, SI5351_CLOCK_OUTPUT);
  si5351.pll_reset(SI5351_PLL);
}

void setFrequencyOffset(double deviation) {
  uint64_t freq = (uint64_t)(((curFreqMHz * 1e6) + deviation) * 100);
  si5351.set_pll(freq * SI5351_CLOCK_DIV, SI5351_PLL);
}

// Busy-waits until the next 200 us sample slot (upstream, incl. the
// micros() overflow case).
void delayForNextSample(uint32_t start) {
  uint32_t sampleDelta = 1000000UL / AUDIO_SAMPLE_RATE_HZ + SAMPLE_WRITE_CORR_US;
  if ((MAX_UINT32 - start) < sampleDelta) {
    while (micros() > start || micros() < (sampleDelta - (MAX_UINT32 - start)))
      ;
  } else {
    while (micros() - start < sampleDelta)
      ;
  }
}

// ---- tone generation (core 1) ----
// One FM sample: look up the sine, scale to the requested deviation, and
// retune the PLL. phase is a 32-bit accumulator; top 8 bits index the table.
void playToneSample(uint32_t& phase, uint32_t phaseStep, uint16_t devHz) {
  uint32_t start = micros();
  phase += phaseStep;
  int16_t s = sineTable[phase >> 24];
  double deviation = ((double)s * devHz) / 32767.0;
  setFrequencyOffset(deviation);
  delayForNextSample(start);
}

uint32_t phaseStepFor(uint16_t toneHz) {
  return (uint32_t)(((uint64_t)toneHz << 32) / AUDIO_SAMPLE_RATE_HZ);
}

// ---- morse ID (core 1, blocking; events announced on serial) ----
const char* morseFor(char c) {
  switch (toupper(c)) {
    case 'A': return ".-";    case 'B': return "-...";  case 'C': return "-.-.";
    case 'D': return "-..";   case 'E': return ".";     case 'F': return "..-.";
    case 'G': return "--.";   case 'H': return "....";  case 'I': return "..";
    case 'J': return ".---";  case 'K': return "-.-";   case 'L': return ".-..";
    case 'M': return "--";    case 'N': return "-.";    case 'O': return "---";
    case 'P': return ".--.";  case 'Q': return "--.-";  case 'R': return ".-.";
    case 'S': return "...";   case 'T': return "-";     case 'U': return "..-";
    case 'V': return "...-";  case 'W': return ".--";   case 'X': return "-..-";
    case 'Y': return "-.--";  case 'Z': return "--..";
    case '0': return "-----"; case '1': return ".----"; case '2': return "..---";
    case '3': return "...--"; case '4': return "....-"; case '5': return ".....";
    case '6': return "-...."; case '7': return "--..."; case '8': return "---..";
    case '9': return "----."; case '/': return "-..-.";
    default: return "";
  }
}

// Keys an element: tone for keyed ms, dead carrier for the rest.
void morseElement(bool keyed, uint32_t ms, uint32_t& phase, uint32_t phaseStep, uint16_t devHz) {
  uint32_t until = millis() + ms;
  while ((int32_t)(millis() - until) < 0) {
    if (keyed) {
      playToneSample(phase, phaseStep, devHz);
    } else {
      uint32_t start = micros();
      setFrequencyOffset(0);
      delayForNextSample(start);
    }
  }
}

// Sends "DE <callsign>" as MCW at ID_WPM. RF must already be on.
void sendMorseId() {
  idActive = true;
  Serial.println("*ID START");
  const uint32_t dit = 1200 / ID_WPM;
  uint32_t phase = 0;
  uint32_t phaseStep = phaseStepFor(ID_TONE_HZ);
  uint16_t devHz = (curMode == RF_TONE && curDevHz > 0) ? curDevHz : 3000;

  char msg[16] = "DE ";
  strncat(msg, callsign, sizeof(msg) - 4);
  for (const char* p = msg; *p; p++) {
    if (*p == ' ') {
      morseElement(false, dit * 7, phase, phaseStep, devHz);
      continue;
    }
    const char* code = morseFor(*p);
    for (; *code; code++) {
      morseElement(true, (*code == '-') ? dit * 3 : dit, phase, phaseStep, devHz);
      morseElement(false, dit, phase, phaseStep, devHz);
    }
    morseElement(false, dit * 2, phase, phaseStep, devHz);  // 3 dits total between chars
  }
  lastIdMillis = millis();
  txSinceId = false;
  idActive = false;
  Serial.println("*ID DONE");
}

// ---- core 1: RF task ----
void setup1() {
  // Core 0 finishes hardware bring-up (Wire, si5351, pins) in setup()
  // before core 1 is released; Arduino-Pico starts both cores itself, so
  // hold here until setup() signals readiness via the ack counter.
  while (ackSeq == 0)
    ;
}

void loop1() {
  // Apply any pending command at a sample boundary.
  if (cmdSeq != ackSeq) {
    Command cmd;
    memcpy(&cmd, (const void*)&mailbox, sizeof(cmd));
    if (cmd.setAtt) {
      // Designer guidance (AI6YM): the attenuator bit-bang is not I2C-safe
      // for the Si5351 while transmitting, so blank the RF around the
      // write and re-tune to restore known PLL/MS register state.
      bool rfWasOn = (curMode != RF_OFF);
      if (rfWasOn) setRf(false);
      programAttenuator(cmd.attenuation);
      curAtt = cmd.attenuation;
      if (rfWasOn) {
        tuneCarrier(curFreqMHz);
        setRf(true);
      }
    }
    if (cmd.setFreq) {
      curFreqMHz = cmd.freqMHz;
      tuneCarrier(curFreqMHz);
    }
    if (cmd.setMode) {
      bool wasOff = (curMode == RF_OFF);
      if (cmd.mode == RF_OFF && curMode != RF_OFF && txSinceId) {
        sendMorseId();  // end-of-transmission ID (97.119(a))
      }
      curMode = cmd.mode;
      curToneHz = cmd.toneHz;
      curDevHz = cmd.devHz;
      if (cmd.mode == RF_OFF) {
        setRf(false);
      } else {
        tuneCarrier(curFreqMHz);
        setRf(true);
        txSinceId = true;
        if (wasOff) lastIdMillis = millis();  // fresh 9-min window
      }
    }
    if (cmd.sendId && curMode != RF_OFF) {
      sendMorseId();
    }
    ackSeq = cmdSeq;
  }

  switch (curMode) {
    case RF_TONE: {
      static uint32_t phase = 0;
      playToneSample(phase, phaseStepFor(curToneHz), curDevHz);
      break;
    }
    case RF_CARRIER: {
      uint32_t start = micros();
      setFrequencyOffset(0);
      delayForNextSample(start);
      break;
    }
    default:
      delayMicroseconds(200);
      break;
  }

  // Automatic ID while transmitting.
  if (curMode != RF_OFF && !idActive && (millis() - lastIdMillis) > ID_INTERVAL_MS) {
    sendMorseId();
  }
}

// ---- core 0: serial command interface ----
// Blocks until core 1 applies the mailbox; during a morse ID (~10 s) that
// is the expected worst case, so the host driver needs a patient timeout.
void submit(Command& cmd) {
  memcpy((void*)&mailbox, &cmd, sizeof(cmd));
  uint32_t seq = cmdSeq + 1;
  cmdSeq = seq;
  while (ackSeq != seq)
    ;
}

void handleLine(char* line) {
  // Tokenize in place.
  char* save = nullptr;
  char* verb = strtok_r(line, " \t", &save);
  if (!verb) return;
  for (char* p = verb; *p; p++) *p = toupper(*p);

  Command cmd = {};

  if (strcmp(verb, "HELLO") == 0 || strcmp(verb, "VER") == 0) {
    Serial.printf("OK FOXSINAD %s REV%u\r\n", FW_VERSION, revision);

  } else if (strcmp(verb, "CALL") == 0) {
    char* arg = strtok_r(nullptr, " \t", &save);
    if (!arg || strlen(arg) >= sizeof(callsign)) {
      Serial.println("ERR CALL <callsign> (1-11 chars)");
      return;
    }
    for (char* p = arg; *p; p++) {
      *p = toupper(*p);
      if (!isalnum(*p) && *p != '/') {
        Serial.println("ERR callsign: letters, digits, / only");
        return;
      }
    }
    strncpy(callsign, arg, sizeof(callsign) - 1);
    Serial.printf("OK CALL %s\r\n", callsign);

  } else if (strcmp(verb, "FREQ") == 0) {
    char* arg = strtok_r(nullptr, " \t", &save);
    double mhz = arg ? atof(arg) : 0;
    double marginMHz = FREQ_MARGIN_HZ / 1e6;
    if (mhz < MIN_FREQ_MHZ + marginMHz || mhz > MAX_FREQ_MHZ - marginMHz) {
      Serial.printf("ERR FREQ %.4f-%.4f MHz\r\n", MIN_FREQ_MHZ + marginMHz,
                    MAX_FREQ_MHZ - marginMHz);
      return;
    }
    cmd.setFreq = true;
    cmd.freqMHz = mhz;
    submit(cmd);
    Serial.printf("OK FREQ %.6f\r\n", mhz);

  } else if (strcmp(verb, "ATT") == 0) {
    char* arg = strtok_r(nullptr, " \t", &save);
    if (revision != 3) {
      Serial.println("ERR no attenuator on this board revision");
      return;
    }
    long att = arg ? atol(arg) : -1;
    if (att < 0 || att > 127) {
      Serial.println("ERR ATT 0-127 (0.25 dB steps)");
      return;
    }
    cmd.setAtt = true;
    cmd.attenuation = (uint8_t)att;
    submit(cmd);
    Serial.printf("OK ATT %ld\r\n", att);

  } else if (strcmp(verb, "TONE") == 0) {
    char* arg1 = strtok_r(nullptr, " \t", &save);
    if (arg1 && strcasecmp(arg1, "OFF") == 0) {
      cmd.setMode = true;
      cmd.mode = RF_OFF;
      submit(cmd);
      Serial.println("OK OFF");
      return;
    }
    if (strlen(callsign) == 0) {
      Serial.println("ERR set CALL first");
      return;
    }
    char* arg2 = strtok_r(nullptr, " \t", &save);
    long hz = arg1 ? atol(arg1) : 0;
    long dev = arg2 ? atol(arg2) : 0;
    if (hz < 100 || hz > 2400) {
      Serial.println("ERR TONE <100-2400 Hz> <dev Hz>");
      return;
    }
    if (dev < 100 || dev > MAX_DEVIATION_HZ) {
      Serial.printf("ERR deviation 100-%d Hz\r\n", MAX_DEVIATION_HZ);
      return;
    }
    cmd.setMode = true;
    cmd.mode = RF_TONE;
    cmd.toneHz = (uint16_t)hz;
    cmd.devHz = (uint16_t)dev;
    submit(cmd);
    Serial.printf("OK TONE %ld %ld\r\n", hz, dev);

  } else if (strcmp(verb, "CARRIER") == 0) {
    char* arg = strtok_r(nullptr, " \t", &save);
    if (arg && strcasecmp(arg, "OFF") == 0) {
      cmd.setMode = true;
      cmd.mode = RF_OFF;
      submit(cmd);
      Serial.println("OK OFF");
      return;
    }
    if (!arg || strcasecmp(arg, "ON") != 0) {
      Serial.println("ERR CARRIER ON|OFF");
      return;
    }
    if (strlen(callsign) == 0) {
      Serial.println("ERR set CALL first");
      return;
    }
    cmd.setMode = true;
    cmd.mode = RF_CARRIER;
    submit(cmd);
    Serial.println("OK CARRIER");

  } else if (strcmp(verb, "OFF") == 0) {
    cmd.setMode = true;
    cmd.mode = RF_OFF;
    submit(cmd);
    Serial.println("OK OFF");

  } else if (strcmp(verb, "ABORT") == 0) {
    // Emergency kill: no end-of-transmission ID. Bench use only.
    txSinceId = false;
    cmd.setMode = true;
    cmd.mode = RF_OFF;
    submit(cmd);
    Serial.println("OK ABORT");

  } else if (strcmp(verb, "ID") == 0) {
    if (curMode == RF_OFF) {
      Serial.println("ERR RF is off");
      return;
    }
    cmd.sendId = true;
    submit(cmd);
    Serial.println("OK ID");

  } else if (strcmp(verb, "STATUS") == 0) {
    const char* modeName = (curMode == RF_TONE)      ? "TONE"
                           : (curMode == RF_CARRIER) ? "CARRIER"
                                                     : "OFF";
    Serial.printf(
        "OK STATUS mode=%s freq=%.6f att=%u tone=%u dev=%u call=%s rev=%u "
        "since_id=%lu up=%lu\r\n",
        modeName, curFreqMHz, curAtt, curToneHz, curDevHz,
        strlen(callsign) ? callsign : "-", revision,
        (millis() - lastIdMillis) / 1000, millis() / 1000);

  } else {
    Serial.printf("ERR unknown command %s\r\n", verb);
  }
}

void setup() {
  Serial.begin(115200);

  // Sine table.
  for (int i = 0; i < 256; i++) {
    sineTable[i] = (int16_t)(32767.0 * sin(2.0 * PI * i / 256.0));
  }

  // Board revision from ID pins (0000 = r2, 1000 = r3).
  pinMode(ID0, INPUT_PULLDOWN);
  pinMode(ID1, INPUT_PULLDOWN);
  pinMode(ID2, INPUT_PULLDOWN);
  pinMode(ID3, INPUT_PULLDOWN);
  bool id0 = digitalRead(ID0), id1 = digitalRead(ID1);
  bool id2 = digitalRead(ID2), id3 = digitalRead(ID3);
  if (!id0 && !id1 && !id2 && !id3) {
    revision = 2;
  } else if (id0 && !id1 && !id2 && !id3) {
    revision = 3;
  } else {
    revision = 0;
  }

  pinMode(AMP_EN, OUTPUT);
  digitalWrite(AMP_EN, HIGH);  // amp off
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  if (revision == 3) {
    programAttenuator(curAtt);  // max attenuation until commanded otherwise
  }

  startWire();
  si5351.init(SI5351_CRYSTAL_LOAD_10PF, 0, 0);  // load as expected by the TCXO
  si5351.drive_strength(SI5351_CLOCK_OUTPUT, SI5351_DRIVE_LEVEL);
  si5351.set_int(SI5351_CLOCK_OUTPUT, 1);  // fixed int MS; fine tuning on the PLL
  tuneCarrier(curFreqMHz);
  setRf(false);

  // Release core 1 (it waits for ackSeq != 0).
  cmdSeq = 1;
  ackSeq = 1;

  Serial.printf("*BOOT FOXSINAD %s REV%u\r\n", FW_VERSION, revision);
}

void loop() {
  static char line[64];
  static size_t len = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (len > 0) {
        line[len] = 0;
        handleLine(line);
        len = 0;
      }
    } else if (len < sizeof(line) - 1) {
      line[len++] = c;
    } else {
      len = 0;  // overlong line: drop it
      Serial.println("ERR line too long");
    }
  }
}
