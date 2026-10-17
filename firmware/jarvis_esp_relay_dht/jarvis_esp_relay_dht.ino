/*
  Jarvis ESP relay + DHT sensor (ESP32 или ESP8266, Arduino IDE / arduino-cli)

  HTTP API (порт 80):
    GET /relay/<1|2>/on   GET /relay/<1|2>/off   GET /relay/<1|2>  -> {"relay":1,"state":"ON"}
    GET /sensor           -> {"temperature":21.5,"humidity":40.2}
  MQTT:
    подписка  jarvis/esp1/relay/<n>/set   (ON / OFF)
    публикует jarvis/esp1/relay/<n>/state (retain), jarvis/esp1/sensor раз в 30 с:
              {"temperature":..,"humidity":..}

  Библиотеки (Менеджер библиотек): "PubSubClient" (Nick O'Leary), "DHT sensor library" (Adafruit)
  + "Adafruit Unified Sensor".
  Подключение: реле 1 -> GPIO 26 (ESP32) / D1 (ESP8266), реле 2 -> GPIO 27 / D2, DHT22 data -> GPIO 4 / D5.
  Многие модули реле включаются НИЗКИМ уровнем — см. RELAY_ACTIVE_LOW.
*/
#if defined(ESP32)
  #include <WiFi.h>
  #include <WebServer.h>
  WebServer server(80);
  const int RELAY_PINS[2] = {26, 27};
  const int DHT_PIN = 4;
#else
  #include <ESP8266WiFi.h>
  #include <ESP8266WebServer.h>
  ESP8266WebServer server(80);
  const int RELAY_PINS[2] = {D1, D2};
  const int DHT_PIN = D5;
#endif
#include <PubSubClient.h>
#include <DHT.h>

// ---------- НАСТРОЙКИ ----------
const char* WIFI_SSID = "MyWiFi";
const char* WIFI_PASS = "password";
const char* MQTT_HOST = "192.168.1.10";   // пусто "" = без MQTT
const int   MQTT_PORT = 1883;
const char* MQTT_USER = "";
const char* MQTT_PASS = "";
const char* BASE      = "jarvis/esp1";
const bool  RELAY_ACTIVE_LOW = true;
#define DHT_TYPE DHT22
// -------------------------------

DHT dht(DHT_PIN, DHT_TYPE);
WiFiClient net;
PubSubClient mqtt(net);
bool relayState[2] = {false, false};
unsigned long lastSensor = 0, lastReconnect = 0;

void publishRelay(int i) {
  if (!mqtt.connected()) return;
  String t = String(BASE) + "/relay/" + String(i + 1) + "/state";
  mqtt.publish(t.c_str(), relayState[i] ? "ON" : "OFF", true);
}

void setRelay(int i, bool on) {
  relayState[i] = on;
  digitalWrite(RELAY_PINS[i], (on ^ RELAY_ACTIVE_LOW) ? HIGH : LOW);
  publishRelay(i);
}

String sensorJson() {
  float t = dht.readTemperature();
  float h = dht.readHumidity();
  if (isnan(t) || isnan(h)) return "{\"error\":\"dht\"}";
  return "{\"temperature\":" + String(t, 1) + ",\"humidity\":" + String(h, 1) + "}";
}

void handleRelay() {
  // путь вида /relay/1/on
  String uri = server.uri();
  int n = uri.substring(7, 8).toInt();
  if (n < 1 || n > 2) { server.send(404, "application/json", "{\"error\":\"relay\"}"); return; }
  if (uri.endsWith("/on")) setRelay(n - 1, true);
  else if (uri.endsWith("/off")) setRelay(n - 1, false);
  else if (uri.endsWith("/toggle")) setRelay(n - 1, !relayState[n - 1]);
  server.send(200, "application/json",
              "{\"relay\":" + String(n) + ",\"state\":\"" + (relayState[n - 1] ? "ON" : "OFF") + "\"}");
}

void onMqtt(char* topic, byte* payload, unsigned int len) {
  String t(topic), msg;
  for (unsigned int i = 0; i < len; i++) msg += (char)payload[i];
  msg.toUpperCase();
  for (int i = 0; i < 2; i++) {
    if (t == String(BASE) + "/relay/" + String(i + 1) + "/set") setRelay(i, msg == "ON" || msg == "1" || msg == "TRUE");
  }
}

void mqttReconnect() {
  if (strlen(MQTT_HOST) == 0 || mqtt.connected() || millis() - lastReconnect < 5000) return;
  lastReconnect = millis();
  String id = "jarvis-esp-" + String((uint32_t)random(0xffffff), HEX);
  if (mqtt.connect(id.c_str(), MQTT_USER, MQTT_PASS)) {
    mqtt.subscribe((String(BASE) + "/relay/+/set").c_str());
    for (int i = 0; i < 2; i++) publishRelay(i);
  }
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < 2; i++) { pinMode(RELAY_PINS[i], OUTPUT); setRelay(i, false); }
  dht.begin();
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.print("\nIP: "); Serial.println(WiFi.localIP());

  for (int n = 1; n <= 2; n++) {
    String b = "/relay/" + String(n);
    server.on(b, handleRelay);
    server.on(b + "/on", handleRelay);
    server.on(b + "/off", handleRelay);
    server.on(b + "/toggle", handleRelay);
  }
  server.on("/sensor", []() { server.send(200, "application/json", sensorJson()); });
  server.on("/", []() { server.send(200, "text/plain", "Jarvis ESP OK"); });
  server.begin();

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqtt);
}

void loop() {
  server.handleClient();
  mqttReconnect();
  mqtt.loop();
  if (mqtt.connected() && millis() - lastSensor > 30000) {
    lastSensor = millis();
    mqtt.publish((String(BASE) + "/sensor").c_str(), sensorJson().c_str(), true);
  }
}
