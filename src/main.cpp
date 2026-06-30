#include <Arduino.h>
#include <ArduinoBLE.h>
#include <SPI.h>
#include <stdlib.h>

namespace {

constexpr uint8_t kMax31855CsPin = 10;
constexpr uint8_t kRelayPin = 7;
constexpr uint32_t kMax31855SpiHz = 4000000;
constexpr unsigned long kSamplePeriodMs = 1000;
constexpr uint8_t kRelayEnergizedLevel = HIGH;  // Relay module H/L selector is set to H.
constexpr uint8_t kRelayIdleLevel = LOW;
constexpr char kBleLocalName[] = "ThermoCouple";
constexpr char kBleServiceUuid[] = "7f3fd100-9a7e-4f4f-a5f1-f6c5437fd801";
constexpr char kBleDataUuid[] = "7f3fd101-9a7e-4f4f-a5f1-f6c5437fd801";
constexpr char kBleCommandUuid[] = "7f3fd102-9a7e-4f4f-a5f1-f6c5437fd801";
constexpr uint16_t kBleDataLength = 300;
constexpr uint8_t kBleCommandLength = 96;
constexpr uint8_t kControlAverageSamples = 5;
constexpr uint16_t kSampleHistoryCapacity = 600;  // 10 minutes at 1 Hz.
constexpr unsigned long kMinRelayDwellMs = 1000;

SPISettings max31855SpiSettings(
    kMax31855SpiHz,
    MSBFIRST,
    SPI_MODE0
);

BLEService thermocoupleService(kBleServiceUuid);
BLEStringCharacteristic bleDataCharacteristic(kBleDataUuid, BLERead | BLENotify, kBleDataLength);
BLEStringCharacteristic bleCommandCharacteristic(kBleCommandUuid, BLEWrite | BLEWriteWithoutResponse, kBleCommandLength);

struct Max31855Reading {
  bool ok;
  double thermocoupleC;
  double internalC;
  uint8_t faultBits;
  uint32_t raw;
};

struct SampleRecord {
  unsigned long sampleMs;
  unsigned long uvOnMs;
  float thermocoupleC;
  float internalC;
  uint32_t raw;
  uint8_t faultBits;
  bool ok;
  bool lampOn;
};

enum class RecipeGoalMode {
  Total,
  Uv,
};

enum class RecipeLastState {
  None,
  Stopped,
  Complete,
};

struct RecipeState {
  bool running = false;
  RecipeGoalMode goalMode = RecipeGoalMode::Total;
  RecipeLastState lastState = RecipeLastState::None;
  double lowerC = 26.0;
  double upperC = 30.0;
  unsigned long durationS = 0;
  unsigned long startMs = 0;
  unsigned long uvOnAccumulatedMs = 0;
  unsigned long uvOnSinceMs = 0;
  bool uvOnTiming = false;
  bool startupHeating = false;
  double controlSamples[kControlAverageSamples] = {};
  uint8_t controlCount = 0;
  double lastControlC = 0.0;
  bool haveControlTemp = false;
  unsigned long lastLampChangeMs = 0;
};

bool lampOn = false;
char commandBuffer[kBleCommandLength];
uint8_t commandLength = 0;
char bleCommandBuffer[kBleCommandLength];
unsigned long lastSampleMs = 0;
bool bleReady = false;
bool bleConnected = false;
RecipeState recipe;
SampleRecord sampleHistory[kSampleHistoryCapacity];
uint16_t sampleHistoryStart = 0;
uint16_t sampleHistoryCount = 0;
unsigned long lastLampSetMs = 0;
const char* lastLampReason = "BOOT";

String sampleLine(
    const __FlashStringHelper* prefix,
    unsigned long sampleMs,
    double thermocoupleC,
    double internalC,
    bool ok,
    uint8_t faultBits,
    uint32_t raw,
    bool sampleLampOn,
    unsigned long uvOnMs);
void emitHistorySince(unsigned long sinceMs);

void writeRelayEnergized(bool energized) {
  digitalWrite(kRelayPin, energized ? kRelayEnergizedLevel : kRelayIdleLevel);
}

void setLamp(bool on, const char* reason = "DIRECT") {
  // The lamp should be wired through COM and NC so it is on when the relay is idle.
  // Energizing the relay opens NC, which turns the lamp off.
  lampOn = on;
  lastLampSetMs = millis();
  lastLampReason = reason;
  writeRelayEnergized(!on);
}

unsigned long elapsedMs(unsigned long startMs, unsigned long nowMs) {
  return nowMs - startMs;
}

unsigned long recipeElapsedMs(unsigned long nowMs) {
  if (!recipe.running) {
    return 0;
  }
  return elapsedMs(recipe.startMs, nowMs);
}

unsigned long currentUvOnMs(unsigned long nowMs) {
  unsigned long total = recipe.uvOnAccumulatedMs;
  if (recipe.uvOnTiming) {
    total += elapsedMs(recipe.uvOnSinceMs, nowMs);
  }
  return total;
}

bool millisAfter(unsigned long value, unsigned long reference) {
  return static_cast<long>(value - reference) > 0;
}

void clearSampleHistory() {
  sampleHistoryStart = 0;
  sampleHistoryCount = 0;
}

uint16_t sampleHistoryIndex(uint16_t offset) {
  return (sampleHistoryStart + offset) % kSampleHistoryCapacity;
}

void storeSampleHistory(const Max31855Reading& reading, unsigned long sampleMs) {
  uint16_t index = sampleHistoryIndex(sampleHistoryCount);
  if (sampleHistoryCount < kSampleHistoryCapacity) {
    ++sampleHistoryCount;
  } else {
    index = sampleHistoryStart;
    sampleHistoryStart = sampleHistoryIndex(1);
  }

  sampleHistory[index] = {
      sampleMs,
      currentUvOnMs(sampleMs),
      static_cast<float>(reading.thermocoupleC),
      static_cast<float>(reading.internalC),
      reading.raw,
      reading.faultBits,
      reading.ok,
      lampOn,
  };
}

const __FlashStringHelper* goalModeName() {
  return recipe.goalMode == RecipeGoalMode::Uv ? F("UV") : F("TOTAL");
}

const __FlashStringHelper* lastStateName() {
  switch (recipe.lastState) {
    case RecipeLastState::Stopped:
      return F("STOPPED");
    case RecipeLastState::Complete:
      return F("COMPLETE");
    case RecipeLastState::None:
    default:
      return F("NONE");
  }
}

unsigned long recipeRemainingS(unsigned long nowMs) {
  const unsigned long targetMs = recipe.durationS * 1000UL;
  const unsigned long progressMs =
      recipe.goalMode == RecipeGoalMode::Uv ? currentUvOnMs(nowMs) : recipeElapsedMs(nowMs);

  if (progressMs >= targetMs) {
    return 0;
  }

  return (targetMs - progressMs + 999UL) / 1000UL;
}

void trackRecipeLampTransition(bool on, unsigned long nowMs) {
  if (on) {
    if (!recipe.uvOnTiming) {
      recipe.uvOnSinceMs = nowMs;
      recipe.uvOnTiming = true;
    }
    return;
  }

  if (recipe.uvOnTiming) {
    recipe.uvOnAccumulatedMs += elapsedMs(recipe.uvOnSinceMs, nowMs);
    recipe.uvOnTiming = false;
  }
}

void setRecipeLamp(bool on, unsigned long nowMs, bool force = false, const char* reason = "CONTROL") {
  if (!force && lampOn == on) {
    return;
  }

  if (!force && on && recipe.lastLampChangeMs != 0 && elapsedMs(recipe.lastLampChangeMs, nowMs) < kMinRelayDwellMs) {
    return;
  }

  if (lampOn != on) {
    trackRecipeLampTransition(on, nowMs);
    recipe.lastLampChangeMs = nowMs;
  }
  setLamp(on, reason);
}

uint32_t readMax31855Raw() {
  SPI.beginTransaction(max31855SpiSettings);
  digitalWrite(kMax31855CsPin, LOW);
  delayMicroseconds(1);

  uint32_t value = 0;
  for (int i = 0; i < 4; ++i) {
    value = (value << 8) | SPI.transfer(0x00);
  }

  digitalWrite(kMax31855CsPin, HIGH);
  SPI.endTransaction();
  return value;
}

double decodeSignedFixedPoint(uint32_t raw, uint8_t shift, uint8_t bits, double lsbC) {
  int32_t value = static_cast<int32_t>((raw >> shift) & ((1UL << bits) - 1));

  if (value & (1UL << (bits - 1))) {
    value -= (1UL << bits);
  }

  return value * lsbC;
}

Max31855Reading readMax31855() {
  const uint32_t raw = readMax31855Raw();
  const uint8_t faultBits = raw & 0x07;

  return {
      faultBits == 0 && ((raw & 0x00010000UL) == 0),
      decodeSignedFixedPoint(raw, 18, 14, 0.25),
      decodeSignedFixedPoint(raw, 4, 12, 0.0625),
      faultBits,
      raw,
  };
}

void appendPaddedHex(String& line, uint32_t value) {
  for (int shift = 28; shift >= 0; shift -= 4) {
    line += String((value >> shift) & 0x0F, HEX);
  }
}

void notifyBleLine(const String& line) {
  if (bleReady && bleConnected) {
    bleDataCharacteristic.writeValue(line);
  }
}

void emitLine(const String& line) {
  Serial.println(line);
  notifyBleLine(line);
}

void emitLine(const __FlashStringHelper* line) {
  Serial.println(line);
  notifyBleLine(String(line));
}

void printAck(const __FlashStringHelper* message) {
  String line(F("ACK,"));
  line += message;
  emitLine(line);
}

void printErr(const __FlashStringHelper* message) {
  String line(F("ERR,"));
  line += message;
  emitLine(line);
}

bool commandsMatch(const char* command, const char* target) {
  return strcmp(command, target) == 0;
}

void emitStatus() {
  const unsigned long nowMs = millis();
  const unsigned long elapsedS = recipe.running ? recipeElapsedMs(nowMs) / 1000UL : 0;
  const unsigned long uvOnS = currentUvOnMs(nowMs) / 1000UL;

  String line(F("STATUS,relay_pin="));
  line += String(kRelayPin);
  line += F(",lamp=");
  line += lampOn ? F("ON") : F("OFF");
  line += F(",last_lamp_ms=");
  line += String(lastLampSetMs);
  line += F(",last_lamp_reason=");
  line += lastLampReason;
  line += F(",ble=");
  line += bleConnected ? F("CONNECTED") : F("DISCONNECTED");
  line += F(",recipe=");
  line += recipe.running ? F("RUNNING") : F("IDLE");
  line += F(",last=");
  line += lastStateName();
  line += F(",mode=");
  line += goalModeName();
  line += F(",lower=");
  line += String(recipe.lowerC, 2);
  line += F(",upper=");
  line += String(recipe.upperC, 2);
  line += F(",duration_s=");
  line += String(recipe.durationS);
  line += F(",elapsed_s=");
  line += String(elapsedS);
  line += F(",uv_on_s=");
  line += String(uvOnS);
  line += F(",remaining_s=");
  line += recipe.running ? String(recipeRemainingS(nowMs)) : String(0);
  line += F(",start_ms=");
  line += String(recipe.startMs);
  line += F(",startup=");
  line += recipe.startupHeating ? F("1") : F("0");
  line += F(",history_count=");
  line += String(sampleHistoryCount);
  line += F(",history_capacity=");
  line += String(kSampleHistoryCapacity);
  emitLine(line);
}

bool parseDoubleValue(const char* text, double& value) {
  if (text == nullptr || *text == '\0') {
    return false;
  }

  char* end = nullptr;
  value = strtod(text, &end);
  return end != text && *end == '\0' && isfinite(value);
}

bool parseUnsignedLongValue(const char* text, unsigned long& value) {
  if (text == nullptr || *text == '\0') {
    return false;
  }

  char* end = nullptr;
  value = strtoul(text, &end, 10);
  return end != text && *end == '\0';
}

bool parseGoalMode(const char* text, RecipeGoalMode& mode) {
  if (text == nullptr) {
    return false;
  }

  if (commandsMatch(text, "TOTAL") || commandsMatch(text, "WALL")) {
    mode = RecipeGoalMode::Total;
    return true;
  }

  if (commandsMatch(text, "UV") || commandsMatch(text, "UV_TIME")) {
    mode = RecipeGoalMode::Uv;
    return true;
  }

  return false;
}

void startRecipe(double lowerC, double upperC, unsigned long durationS, RecipeGoalMode mode) {
  const unsigned long nowMs = millis();

  recipe.running = true;
  recipe.goalMode = mode;
  recipe.lastState = RecipeLastState::None;
  recipe.lowerC = lowerC;
  recipe.upperC = upperC;
  recipe.durationS = durationS;
  recipe.startMs = nowMs;
  recipe.uvOnAccumulatedMs = 0;
  recipe.uvOnSinceMs = 0;
  recipe.uvOnTiming = false;
  recipe.startupHeating = true;
  recipe.controlCount = 0;
  recipe.lastControlC = 0.0;
  recipe.haveControlTemp = false;
  recipe.lastLampChangeMs = 0;

  clearSampleHistory();
  setRecipeLamp(false, nowMs, true, "START_OFF");
  printAck(F("RECIPE_START"));
  emitStatus();
}

void emitRecipeDone(RecipeLastState lastState) {
  const unsigned long nowMs = millis();
  String line(F("RECIPE,"));
  line += lastState == RecipeLastState::Complete ? F("DONE") : F("STOPPED");
  line += F(",elapsed_s=");
  line += String(recipeElapsedMs(nowMs) / 1000UL);
  line += F(",uv_on_s=");
  line += String(currentUvOnMs(nowMs) / 1000UL);
  emitLine(line);
}

void finishRecipe(RecipeLastState lastState) {
  if (!recipe.running) {
    recipe.lastState = lastState;
    setLamp(false, "STOP_IDLE");
    emitStatus();
    return;
  }

  const unsigned long nowMs = millis();
  setRecipeLamp(false, nowMs, true, lastState == RecipeLastState::Complete ? "COMPLETE" : "STOP");
  emitRecipeDone(lastState);
  recipe.running = false;
  recipe.startupHeating = false;
  recipe.lastState = lastState;
  emitStatus();
}

bool updateControlFilter(const Max31855Reading& reading) {
  if (!reading.ok || !isfinite(reading.thermocoupleC)) {
    recipe.haveControlTemp = false;
    return false;
  }

  if (recipe.controlCount < kControlAverageSamples) {
    recipe.controlSamples[recipe.controlCount++] = reading.thermocoupleC;
  } else {
    for (uint8_t i = 1; i < kControlAverageSamples; ++i) {
      recipe.controlSamples[i - 1] = recipe.controlSamples[i];
    }
    recipe.controlSamples[kControlAverageSamples - 1] = reading.thermocoupleC;
  }

  double sum = 0.0;
  for (uint8_t i = 0; i < recipe.controlCount; ++i) {
    sum += recipe.controlSamples[i];
  }

  recipe.lastControlC = sum / recipe.controlCount;
  recipe.haveControlTemp = true;
  return true;
}

void applyRecipeControl(const Max31855Reading& reading, unsigned long sampleMs) {
  if (!recipe.running) {
    return;
  }

  const unsigned long targetMs = recipe.durationS * 1000UL;
  if (recipe.goalMode == RecipeGoalMode::Total && recipeElapsedMs(sampleMs) >= targetMs) {
    finishRecipe(RecipeLastState::Complete);
    return;
  }

  const bool haveControlTemp = updateControlFilter(reading);

  if (!reading.ok) {
    recipe.startupHeating = false;
    setRecipeLamp(false, sampleMs, true, "FAULT");
  } else if (!haveControlTemp) {
    setRecipeLamp(false, sampleMs, true, "NO_CONTROL");
  } else if (recipe.startupHeating) {
    if (recipe.lastControlC >= recipe.upperC) {
      recipe.startupHeating = false;
      setRecipeLamp(false, sampleMs, true, "UPPER");
    } else {
      setRecipeLamp(true, sampleMs, false, "WARMUP");
    }
  } else if (recipe.lastControlC <= recipe.lowerC) {
    setRecipeLamp(true, sampleMs, false, "LOWER");
  } else if (recipe.lastControlC >= recipe.upperC) {
    setRecipeLamp(false, sampleMs, false, "UPPER");
  }

  if (recipe.running && recipe.goalMode == RecipeGoalMode::Uv && currentUvOnMs(sampleMs) >= targetMs) {
    finishRecipe(RecipeLastState::Complete);
  }
}

void processRecipeStart(char* args) {
  if (args == nullptr) {
    printErr(F("BAD_RECIPE_START"));
    return;
  }

  double lowerC = 0.0;
  double upperC = 0.0;
  unsigned long durationS = 0;
  RecipeGoalMode mode = RecipeGoalMode::Total;

  char* lowerText = strtok(args, ",");
  char* upperText = strtok(nullptr, ",");
  char* durationText = strtok(nullptr, ",");
  char* modeText = strtok(nullptr, ",");

  if (!parseDoubleValue(lowerText, lowerC) ||
      !parseDoubleValue(upperText, upperC) ||
      !parseUnsignedLongValue(durationText, durationS) ||
      !parseGoalMode(modeText, mode)) {
    printErr(F("BAD_RECIPE_START"));
    return;
  }

  if (lowerC >= upperC || durationS == 0) {
    printErr(F("BAD_RECIPE_LIMITS"));
    return;
  }

  startRecipe(lowerC, upperC, durationS, mode);
}

void processHistorySince(char* args) {
  unsigned long sinceMs = 0;
  if (!parseUnsignedLongValue(args, sinceMs)) {
    printErr(F("BAD_HISTORY_SINCE"));
    return;
  }

  emitHistorySince(sinceMs);
}

void processCommand(char* command) {
  for (char* cursor = command; *cursor != '\0'; ++cursor) {
    if (*cursor >= 'a' && *cursor <= 'z') {
      *cursor -= 32;
    } else if (*cursor == ' ') {
      *cursor = '_';
    }
  }

  char* args = strchr(command, ',');
  if (args != nullptr) {
    *args = '\0';
    ++args;
  }

  if (commandsMatch(command, "RECIPE_START") || commandsMatch(command, "START_RECIPE")) {
    processRecipeStart(args);
  } else if (commandsMatch(command, "RECIPE_STOP") || commandsMatch(command, "STOP_RECIPE")) {
    finishRecipe(RecipeLastState::Stopped);
    printAck(F("RECIPE_STOP"));
  } else if (commandsMatch(command, "HISTORY_SINCE")) {
    processHistorySince(args);
  } else if (commandsMatch(command, "LAMP_ON") || commandsMatch(command, "ON")) {
    if (recipe.running) {
      printErr(F("RECIPE_RUNNING"));
    } else {
      setLamp(true, "MANUAL_ON");
      printAck(F("LAMP_ON"));
    }
  } else if (commandsMatch(command, "LAMP_OFF") || commandsMatch(command, "OFF")) {
    if (recipe.running) {
      printErr(F("RECIPE_RUNNING"));
    } else {
      setLamp(false, "MANUAL_OFF");
      printAck(F("LAMP_OFF"));
    }
  } else if (commandsMatch(command, "STATUS") || commandsMatch(command, "RECIPE_STATUS")) {
    emitStatus();
  } else if (command[0] != '\0') {
    String line(F("ERR,UNKNOWN_COMMAND,"));
    line += command;
    emitLine(line);
  }
}

void handleSerialCommands() {
  while (Serial.available() > 0) {
    const char incoming = static_cast<char>(Serial.read());

    if (incoming == '\r') {
      continue;
    }

    if (incoming == '\n') {
      commandBuffer[commandLength] = '\0';
      processCommand(commandBuffer);
      commandLength = 0;
      continue;
    }

    if (commandLength < sizeof(commandBuffer) - 1) {
      commandBuffer[commandLength++] = incoming;
    }
  }
}

void handleBleCommands() {
  if (!bleReady) {
    return;
  }

  BLE.poll();

  if (!bleCommandCharacteristic.written()) {
    return;
  }

  String command = bleCommandCharacteristic.value();
  command.trim();
  command.toCharArray(bleCommandBuffer, sizeof(bleCommandBuffer));
  processCommand(bleCommandBuffer);
}

String sampleLine(
    const __FlashStringHelper* prefix,
    unsigned long sampleMs,
    double thermocoupleC,
    double internalC,
    bool ok,
    uint8_t faultBits,
    uint32_t raw,
    bool sampleLampOn,
    unsigned long uvOnMs) {
  String line(prefix);
  line.reserve(kBleDataLength);
  line += ',';
  line += sampleMs;
  line += ',';
  line += String(thermocoupleC, 2);
  line += ',';
  line += String(internalC, 2);
  line += ',';
  line += ok ? '1' : '0';
  line += ',';
  line += String(faultBits);
  line += F(",0x");
  appendPaddedHex(line, raw);
  line += ',';
  line += sampleLampOn ? F("ON") : F("OFF");
  line += ',';
  line += String(uvOnMs);
  return line;
}

void emitHistorySince(unsigned long sinceMs) {
  const bool hasHistory = sampleHistoryCount > 0;
  const unsigned long oldestMs = hasHistory ? sampleHistory[sampleHistoryStart].sampleMs : 0;
  const unsigned long newestMs =
      hasHistory ? sampleHistory[sampleHistoryIndex(sampleHistoryCount - 1)].sampleMs : 0;
  const bool wrapped = sampleHistoryCount == kSampleHistoryCapacity;
  const bool lost = wrapped && millisAfter(oldestMs, sinceMs);

  String begin(F("HISTORY_BEGIN,since_ms="));
  begin += String(sinceMs);
  begin += F(",oldest_ms=");
  begin += String(oldestMs);
  begin += F(",newest_ms=");
  begin += String(newestMs);
  begin += F(",count=");
  begin += String(sampleHistoryCount);
  begin += F(",capacity=");
  begin += String(kSampleHistoryCapacity);
  begin += F(",lost=");
  begin += lost ? F("1") : F("0");
  emitLine(begin);

  uint16_t sent = 0;
  for (uint16_t offset = 0; offset < sampleHistoryCount; ++offset) {
    const SampleRecord& record = sampleHistory[sampleHistoryIndex(offset)];
    if (!millisAfter(record.sampleMs, sinceMs)) {
      continue;
    }

    emitLine(sampleLine(
        F("HIST"),
        record.sampleMs,
        record.thermocoupleC,
        record.internalC,
        record.ok,
        record.faultBits,
        record.raw,
        record.lampOn,
        record.uvOnMs));
    ++sent;

    if (sent % 4 == 0) {
      BLE.poll();
      delay(2);
    }
  }

  String end(F("HISTORY_END,sent="));
  end += String(sent);
  emitLine(end);
}

void printReading(const Max31855Reading& reading, unsigned long sampleMs) {
  emitLine(sampleLine(
      F("DATA"),
      sampleMs,
      reading.thermocoupleC,
      reading.internalC,
      reading.ok,
      reading.faultBits,
      reading.raw,
      lampOn,
      currentUvOnMs(sampleMs)));
}

void handleBleConnected(BLEDevice central) {
  bleConnected = true;
  Serial.print(F("BLE,CONNECTED,central="));
  Serial.println(central.address());
  bleDataCharacteristic.writeValue("READY");
  emitStatus();
}

void handleBleDisconnected(BLEDevice central) {
  bleConnected = false;
  Serial.print(F("BLE,DISCONNECTED,central="));
  Serial.println(central.address());

  if (recipe.running) {
    Serial.println(F("BLE,RECIPE_CONTINUES"));
  } else {
    Serial.println(F("BLE,STATE_HELD"));
  }

  if (BLE.advertise()) {
    Serial.println(F("BLE,ADVERTISING"));
  } else {
    Serial.println(F("BLE,ADVERTISE_FAILED"));
  }
}

void setupBle() {
  if (!BLE.begin()) {
    Serial.println(F("BLE,INIT_FAILED"));
    return;
  }

  BLE.setLocalName(kBleLocalName);
  BLE.setDeviceName(kBleLocalName);
  BLE.setAdvertisedService(thermocoupleService);
  thermocoupleService.addCharacteristic(bleDataCharacteristic);
  thermocoupleService.addCharacteristic(bleCommandCharacteristic);
  BLE.addService(thermocoupleService);
  BLE.setEventHandler(BLEConnected, handleBleConnected);
  BLE.setEventHandler(BLEDisconnected, handleBleDisconnected);
  bleDataCharacteristic.writeValue("READY");
  bleCommandCharacteristic.writeValue("");

  bleReady = true;
  if (BLE.advertise()) {
    Serial.print(F("BLE,ADVERTISING,name="));
    Serial.println(kBleLocalName);
  } else {
    Serial.println(F("BLE,ADVERTISE_FAILED"));
  }
}

}  // namespace

void setup() {
  digitalWrite(kRelayPin, kRelayIdleLevel);
  pinMode(kRelayPin, OUTPUT);
  setLamp(true, "BOOT");

  pinMode(kMax31855CsPin, OUTPUT);
  digitalWrite(kMax31855CsPin, HIGH);

  Serial.begin(115200);
  while (!Serial && millis() < 3000) {
    delay(10);
  }

  SPI.begin();
  setupBle();

  Serial.println(F("UNO R4 WiFi MAX31855 thermocouple reader"));
  Serial.println(F("CS=D10, DO/SO=CIPO, CLK/SCK=RSPCKA"));
  Serial.println(F("Relay signal=D7, lamp defaults ON"));
  Serial.println(F("READY"));
}

void loop() {
  handleBleCommands();
  handleSerialCommands();

  const unsigned long now = millis();
  if (lastSampleMs == 0 || elapsedMs(lastSampleMs, now) >= kSamplePeriodMs) {
    lastSampleMs = now;
    const Max31855Reading reading = readMax31855();
    const bool wasRecipeRunning = recipe.running;
    applyRecipeControl(reading, now);
    if (wasRecipeRunning || recipe.running) {
      storeSampleHistory(reading, now);
    }
    printReading(reading, now);
  }
}
