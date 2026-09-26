// =====================================
// ALISTO ESP32 V8.7
// SOS + CANCEL + LED + STATUS LED + PCM AUDIO
// Raspberry Pi -> USB Serial -> ESP32
// ESP32 -> I2S -> MAX98357A
// NO BUZZER
//
// V8.7 change: added a SEPARATE status LED (STATUS_LED_PIN, GPIO 27) driven
// by the Pi's voice-listening loop (solid = listening, blinking =
// transcribing). Kept fully independent from the existing SOS/emergency
// LED_PIN so the two never get visually confused with each other.
// =====================================

#include "driver/i2s.h"


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


  Serial.println(
    "ALISTO V8.7 READY"
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
