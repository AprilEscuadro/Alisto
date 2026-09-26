// =====================================
// ALISTO ESP32 V8.9
// SOS + CANCEL + LED + STATUS LED + PCM AUDIO + RTC
// Raspberry Pi -> USB Serial -> ESP32
// ESP32 -> I2S -> MAX98357A
// NO BUZZER
//
// V8.9 change: bumped the serial RX buffer from the tiny 256-byte default
// up to 8192 bytes (Serial.setRxBufferSize before Serial.begin). After the
// Pi was upgraded from a 3B+ to a 4B, the faster Pi pushed audio bytes over
// serial fast enough to overflow the 256-byte buffer -> dropped bytes ->
// audio that started clean then turned into sped-up harsh distortion. The
// Pi-side pacing delay was raised to compensate but that overshot into
// starvation (choppy audio with gaps). A big RX buffer absorbs the fast
// bursts, and i2s_write(..., portMAX_DELAY) still provides real backpressure
// at the true 16kHz playback rate -- so the Pi can send fast again with
// neither distortion nor choppiness. Pair this with a small Pi-side sleep.
//
// V8.7 change: added a SEPARATE status LED (STATUS_LED_PIN, GPIO 27) driven
// by the Pi's voice-listening loop (solid = listening, blinking =
// transcribing). Kept fully independent from the existing SOS/emergency
// LED_PIN so the two never get visually confused with each other.
//
// V8.8 change: added a DS3231 battery-backed RTC over I2C (SDA=21, SCL=22,
// the ESP32's default I2C pins, unused by anything else in this firmware).
// The Pi has no onboard RTC and this whole system is meant to run fully
// offline (no NTP), so its system clock would be wrong after any reboot
// without one. New commands: CMD:GET_TIME (replies "TIME:YYYY-MM-DD
// HH:MM:SS" read from the DS3231, for the Pi to set its own clock from at
// every boot) and CMD:SET_TIME:YYYY-MM-DD HH:MM:SS (writes into the DS3231,
// meant to be used ONCE while the Pi still has a correct clock, e.g. while
// it has internet during development/setup -- replies "RTC_SET_OK").
// Bare Wire.h register read/write, no external RTC library needed.
// =====================================

#include "driver/i2s.h"
#include <Wire.h>


// =====================================
// BUTTONS
// =====================================

#define SOS_BUTTON     4
#define CANCEL_BUTTON 18


// =====================================
// LED (SOS / emergency state)
// =====================================

#define LED_PIN 2


// =====================================
// STATUS LED (voice listening/transcribing indicator -- separate from the
// SOS LED above, driven by CMD:STATUS_LED_ON / CMD:STATUS_LED_OFF)
// =====================================

#define STATUS_LED_PIN 27


// =====================================
// MAX98357A I2S
// =====================================

#define I2S_BCLK 26
#define I2S_LRC  25
#define I2S_DIN  32


// =====================================
// DS3231 RTC (I2C)
// =====================================

#define RTC_SDA 21
#define RTC_SCL 22
#define DS3231_ADDR 0x68


// =====================================
// AUDIO
// =====================================

#define AUDIO_CHUNK_SIZE 1024


uint8_t audioBuffer[AUDIO_CHUNK_SIZE];


bool emergency = false;

bool audioMode = false;


uint32_t audioRemaining = 0;


String commandBuffer = "";



// =====================================
// I2S SETUP
// =====================================

void setupI2S() {


  i2s_config_t config = {


    .mode =
      (i2s_mode_t)(
        I2S_MODE_MASTER |
        I2S_MODE_TX
      ),


    .sample_rate = 16000,


    .bits_per_sample =
      I2S_BITS_PER_SAMPLE_16BIT,


    .channel_format =
      I2S_CHANNEL_FMT_ONLY_LEFT,


    .communication_format =
      I2S_COMM_FORMAT_STAND_I2S,


    .intr_alloc_flags = 0,


    .dma_buf_count = 16,


    .dma_buf_len = 256,


    .use_apll = false,


    .tx_desc_auto_clear = true

  };


  i2s_pin_config_t pins = {


    .bck_io_num = I2S_BCLK,


    .ws_io_num = I2S_LRC,


    .data_out_num = I2S_DIN,


    .data_in_num = -1

  };


  i2s_driver_install(
    I2S_NUM_0,
    &config,
    0,
    NULL
  );


  i2s_set_pin(
    I2S_NUM_0,
    &pins
  );


  i2s_zero_dma_buffer(
    I2S_NUM_0
  );

}



// =====================================
// RTC (DS3231)
// =====================================

byte bcdToDec(byte val) {

  return (
    (val / 16 * 10) +
    (val % 16)
  );

}


byte decToBcd(byte val) {

  return (
    (val / 10 * 16) +
    (val % 10)
  );

}


void setupRTC() {

  Wire.begin(
    RTC_SDA,
    RTC_SCL
  );

}


String readRTCTime() {


  Wire.beginTransmission(
    DS3231_ADDR
  );

  Wire.write(0x00);

  Wire.endTransmission();


  Wire.requestFrom(
    DS3231_ADDR,
    7
  );


  byte second =
    bcdToDec(
      Wire.read() & 0x7F
    );

  byte minute =
    bcdToDec(
      Wire.read()
    );

  byte hour =
    bcdToDec(
      Wire.read() & 0x3F
    );

  Wire.read();  // day-of-week, unused

  byte day =
    bcdToDec(
      Wire.read()
    );

  byte month =
    bcdToDec(
      Wire.read() & 0x1F
    );

  byte year =
    bcdToDec(
      Wire.read()
    );


  char buf[24];

  sprintf(
    buf,
    "20%02d-%02d-%02d %02d:%02d:%02d",
    year, month, day, hour, minute, second
  );


  return String(buf);

}


void setRTCTime(
  int year, int month, int day,
  int hour, int minute, int second
) {


  Wire.beginTransmission(
    DS3231_ADDR
  );

  Wire.write(0x00);

  Wire.write(decToBcd(second));

  Wire.write(decToBcd(minute));

  Wire.write(decToBcd(hour));   // 24-hour mode (bit 6 = 0)

  Wire.write(decToBcd(1));      // day-of-week, arbitrary/unused

  Wire.write(decToBcd(day));

  Wire.write(decToBcd(month));

  Wire.write(decToBcd(year % 100));

  Wire.endTransmission();

}



// =====================================
// EMERGENCY
// =====================================

void activateEmergency() {


  if (emergency) {
    return;
  }


  emergency = true;


  digitalWrite(
    LED_PIN,
    HIGH
  );


  Serial.println(
    "EVENT:EMERGENCY_ACTIVE"
  );

}



// =====================================
// CANCEL
// =====================================

void cancelEmergency() {


  emergency = false;


  audioMode = false;

  audioRemaining = 0;


  digitalWrite(
    LED_PIN,
    LOW
  );


  i2s_zero_dma_buffer(
    I2S_NUM_0
  );


  Serial.println(
    "EVENT:CANCEL"
  );

}



// =====================================
// AUDIO RECEIVE / PLAY
// =====================================

void playAudioStream() {


  if (audioRemaining == 0) {


    audioMode = false;


    delay(100);


    Serial.println(
      "AUDIO_DONE"
    );


    return;
  }


  if (!Serial.available()) {

    return;
  }


  uint32_t amount =
    AUDIO_CHUNK_SIZE;


  if (audioRemaining < amount) {

    amount =
      audioRemaining;

  }


  int available =
    Serial.available();


  if (available < amount) {

    amount =
      available;

  }


  // Keep sample alignment for 16-bit PCM

  amount &= ~1;


  if (amount == 0) {

    return;

  }


  int received =
    Serial.readBytes(
      audioBuffer,
      amount
    );


  if (received <= 0) {

    return;

  }


  size_t written = 0;


  i2s_write(
    I2S_NUM_0,
    audioBuffer,
    received,
    &written,
    portMAX_DELAY
  );


  if (received <= audioRemaining) {

    audioRemaining -= received;

  }
  else {

    audioRemaining = 0;

  }


  if (audioRemaining == 0) {


    // Do NOT clear the DMA buffer here.
    // Let the final samples finish playing.

    delay(100);


    audioMode = false;


    Serial.println(
      "AUDIO_DONE"
    );

  }

}



// =====================================
// COMMAND HANDLER
// =====================================

void handleCommand(
  String cmd
) {


  cmd.trim();



  if (
    cmd ==
    "CMD:VOICE_EMERGENCY"
  ) {


    activateEmergency();

  }



  else if (
    cmd ==
    "CMD:CANCEL"
  ) {


    cancelEmergency();

  }



  else if (
    cmd ==
    "CMD:LED_ON"
  ) {


    digitalWrite(
      LED_PIN,
      HIGH
    );

  }



  else if (
    cmd ==
    "CMD:LED_OFF"
  ) {


    digitalWrite(
      LED_PIN,
      LOW
    );

  }



  else if (
    cmd ==
    "CMD:STATUS_LED_ON"
  ) {


    digitalWrite(
      STATUS_LED_PIN,
      HIGH
    );

  }



  else if (
    cmd ==
    "CMD:STATUS_LED_OFF"
  ) {


    digitalWrite(
      STATUS_LED_PIN,
      LOW
    );

  }



  else if (
    cmd ==
    "CMD:GET_TIME"
  ) {


    Serial.println(
      "TIME:" + readRTCTime()
    );

  }



  else if (
    cmd.startsWith(
      "CMD:SET_TIME:"
    )
  ) {


    // Expected format: CMD:SET_TIME:YYYY-MM-DD HH:MM:SS

    String t =
      cmd.substring(13);


    int year   = t.substring(0, 4).toInt();
    int month  = t.substring(5, 7).toInt();
    int day    = t.substring(8, 10).toInt();
    int hour   = t.substring(11, 13).toInt();
    int minute = t.substring(14, 16).toInt();
    int second = t.substring(17, 19).toInt();


    setRTCTime(
      year, month, day,
      hour, minute, second
    );


    Serial.println(
      "RTC_SET_OK"
    );

  }



  else if (
    cmd.startsWith(
      "AUDIO_START:"
    )
  ) {


    String sizeText =
      cmd.substring(12);


    audioRemaining =
      sizeText.toInt();


    if (audioRemaining > 0) {


      // Clear audio from previous playback

      i2s_zero_dma_buffer(
        I2S_NUM_0
      );


      audioMode = true;


      Serial.println(
        "AUDIO_READY"
      );

    }

  }

}



// =====================================
// SETUP
// =====================================

void setup() {


  // MUST be before Serial.begin() to take effect. Default is only 256
  // bytes -- far too small for the burst of audio bytes a Pi 4B pushes.
  Serial.setRxBufferSize(
    8192
  );


  Serial.begin(
    921600
  );


  Serial.setTimeout(
    50
  );


  pinMode(
    SOS_BUTTON,
    INPUT_PULLUP
  );


  pinMode(
    CANCEL_BUTTON,
    INPUT_PULLUP
  );


  pinMode(
    LED_PIN,
    OUTPUT
  );


  digitalWrite(
    LED_PIN,
    LOW
  );


  pinMode(
    STATUS_LED_PIN,
    OUTPUT
  );


  digitalWrite(
    STATUS_LED_PIN,
    LOW
  );


  setupI2S();


  setupRTC();


  Serial.println(
    "ALISTO V8.9 READY"
  );

}



// =====================================
// LOOP
// =====================================

void loop() {


  // ==================================
  // CANCEL HAS HIGHEST PRIORITY
  // ==================================

  if (
    digitalRead(CANCEL_BUTTON)
    == LOW
  ) {


    cancelEmergency();


    while (
      digitalRead(CANCEL_BUTTON)
      == LOW
    ) {

      delay(10);

    }


    delay(30);


    return;

  }



  // ==================================
  // AUDIO MODE
  // ==================================

  if (audioMode) {


    playAudioStream();


    return;

  }



  // ==================================
  // SOS BUTTON
  // ==================================

  if (
    digitalRead(SOS_BUTTON)
    == LOW
  ) {


    Serial.println(
      "EVENT:SOS"
    );


    activateEmergency();


    while (
      digitalRead(SOS_BUTTON)
      == LOW
    ) {

      delay(10);

    }


    delay(30);

  }



  // ==================================
  // SERIAL COMMAND MODE
  // ==================================

  while (
    Serial.available()
  ) {


    char c =
      Serial.read();


    if (
      c == '\n'
    ) {


      handleCommand(
        commandBuffer
      );


      commandBuffer = "";

    }

    else if (
      c != '\r'
    ) {


      // Prevent runaway String growth

      if (
        commandBuffer.length()
        < 100
      ) {


        commandBuffer += c;

      }

      else {


        commandBuffer = "";

      }

    }

  }

}
