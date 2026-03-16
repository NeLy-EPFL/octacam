// 28BYJ-48 + ULN2003
// Serial input modes:
//
// Mode A (5 floats):
//   rpm, move_duration, rest_duration, total_duration, wait_time
//   - Wait wait_time seconds
//   - Repeat N = ceil(total_duration / (move_duration + rest_duration)) cycles
//   - Per cycle: CCW move_duration -> rest_duration -> CW move_duration ->
//   rest_duration
//
// Mode B (1 float):
//   degrees
//   - Rotate by that many degrees immediately
//   - Positive = CCW, Negative = CW
//   - Uses last configured RPM (default 8.0 at startup)
//
// Input examples:
//   8.0,2.5,1.0,30,5
//   90
//   -180

#include <math.h>
#include <stdlib.h>

const int IN1 = 8;
const int IN2 = 9;
const int IN3 = 10;
const int IN4 = 11;

// More accurate practical value for 28BYJ-48 half-step output revolution
const float HALF_STEPS_PER_REV = 4076.0f;

// Half-step sequence (IN1, IN2, IN3, IN4)
const uint8_t halfStepSeq[8][4] = {{1, 0, 0, 0}, {1, 1, 0, 0}, {0, 1, 0, 0},
                                   {0, 1, 1, 0}, {0, 0, 1, 0}, {0, 0, 1, 1},
                                   {0, 0, 0, 1}, {1, 0, 0, 1}};

int stepIndex = 0;
unsigned long stepDelayUs = 1200;
float currentRpm =
    8.0f; // used for degree mode unless updated by 5-value command

// Parsed command values
float rpmCmd = 0.0f;
float moveDurationS = 0.0f;
float restDurationS = 0.0f;
float totalDurationS = 0.0f;
float waitTimeS = 0.0f;

void setCoils(uint8_t a, uint8_t b, uint8_t c, uint8_t d) {
  digitalWrite(IN1, a);
  digitalWrite(IN2, b);
  digitalWrite(IN3, c);
  digitalWrite(IN4, d);
}

void releaseMotor() { setCoils(0, 0, 0, 0); }

void setRPM(float rpm) {
  if (rpm < 0.1f)
    rpm = 0.1f; // prevent divide-by-zero / too slow extremes
  currentRpm = rpm;

  float stepsPerSecond = (rpm * HALF_STEPS_PER_REV) / 60.0f;
  stepDelayUs = (unsigned long)(1000000.0f / stepsPerSecond);

  // Optional floor to reduce stalling risk; tune for your motor/load
  if (stepDelayUs < 500)
    stepDelayUs = 500;
}

void oneHalfStep(int dir) {
  stepIndex += dir;
  if (stepIndex > 7)
    stepIndex = 0;
  if (stepIndex < 0)
    stepIndex = 7;

  setCoils(halfStepSeq[stepIndex][0], halfStepSeq[stepIndex][1],
           halfStepSeq[stepIndex][2], halfStepSeq[stepIndex][3]);
}

void moveForDuration(float durationS, int dir) {
  if (durationS <= 0)
    return;

  unsigned long segmentUs = (unsigned long)(durationS * 1000000.0f);
  unsigned long movedUs = 0;

  while (movedUs < segmentUs) {
    oneHalfStep(dir);
    delayMicroseconds(stepDelayUs);
    movedUs += stepDelayUs;
  }
}

void restForDuration(float durationS) {
  if (durationS <= 0)
    return;
  unsigned long ms = (unsigned long)(durationS * 1000.0f);
  delay(ms);
}

void moveDegrees(float degrees) {
  if (degrees == 0.0f)
    return;

  int dir = (degrees > 0.0f) ? +1 : -1;
  float absDeg = fabs(degrees);

  // steps = degrees / 360 * HALF_STEPS_PER_REV
  unsigned long steps =
      (unsigned long)round((absDeg / 360.0f) * HALF_STEPS_PER_REV);

  for (unsigned long i = 0; i < steps; i++) {
    oneHalfStep(dir);
    delayMicroseconds(800);
  }

  releaseMotor();
}

int countTokens(String line) {
  line.trim();
  if (line.length() == 0)
    return 0;

  line.replace(',', ' ');

  char buf[120];
  line.toCharArray(buf, sizeof(buf));

  int count = 0;
  char *token = strtok(buf, " ");
  while (token != NULL) {
    count++;
    token = strtok(NULL, " ");
  }
  return count;
}

bool parseOneFloat(String line, float &a) {
  line.trim();
  if (line.length() == 0)
    return false;
  line.replace(',', ' ');

  char buf[40];
  line.toCharArray(buf, sizeof(buf));

  char *token = strtok(buf, " ");
  if (!token)
    return false;
  a = atof(token);

  // ensure there is no second token
  token = strtok(NULL, " ");
  if (token)
    return false;

  return true;
}

bool parseFiveFloats(String line, float &a, float &b, float &c, float &d,
                     float &e) {
  line.trim();
  if (line.length() == 0)
    return false;

  // Allow commas by converting them to spaces
  line.replace(',', ' ');

  char buf[120];
  line.toCharArray(buf, sizeof(buf));

  char *token = strtok(buf, " ");
  if (!token)
    return false;
  a = atof(token);

  token = strtok(NULL, " ");
  if (!token)
    return false;
  b = atof(token);

  token = strtok(NULL, " ");
  if (!token)
    return false;
  c = atof(token);

  token = strtok(NULL, " ");
  if (!token)
    return false;
  d = atof(token);

  token = strtok(NULL, " ");
  if (!token)
    return false;
  e = atof(token);

  // ensure there is no 6th token
  token = strtok(NULL, " ");
  if (token)
    return false;

  return true;
}

void runProfile(float rpm, float moveS, float restS, float totalS,
                float waitS) {
  if (rpm <= 0 || moveS < 0 || restS < 0 || totalS <= 0 || waitS < 0) {
    Serial.println("Invalid values. Need: rpm>0, move/rest/wait>=0, total>0");
    return;
  }

  float denom = moveS + restS;
  if (denom <= 0.0f) {
    // Serial.println("Invalid values. move_duration + rest_duration must be >
    // 0");
    return;
  }

  unsigned long repeatCount = (unsigned long)ceil(totalS / denom);

  setRPM(rpm);

  //  Serial.println("Command received.");
  //  Serial.print("Waiting "); Serial.print(waitS, 3); Serial.println("s before
  //  start...");
  delay((unsigned long)(waitS * 1000.0f));

  //  Serial.println("Starting profile...");
  //  Serial.print("RPM="); Serial.print(rpm, 6);
  //  Serial.print(" move="); Serial.print(moveS, 6);
  //  Serial.print("s rest="); Serial.print(restS, 6);
  //  Serial.print("s total="); Serial.print(totalS, 6);
  //  Serial.print("s wait="); Serial.print(waitS, 6);
  //  Serial.print("s repeats="); Serial.println(repeatCount);

  for (unsigned long i = 0; i < repeatCount; i++) {
    if (i % 2) {
      // CCW
      moveForDuration(moveS, +1);
    } else {
      // CW
      moveForDuration(moveS, -1);
    }
    // Rest
    releaseMotor();
    restForDuration(restS);
  }

  releaseMotor();
  //  Serial.println("Profile complete. Motor stopped.");
}

void setup() {
  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);
  pinMode(IN3, OUTPUT);
  pinMode(IN4, OUTPUT);

  releaseMotor();

  Serial.begin(9600);
  while (!Serial) {
    ;
  }

  setRPM(currentRpm); // initialize stepDelayUs from default RPM

  //  Serial.println("Input modes:");
  //  Serial.println("1) 5 values:
  //  rpm,move_duration,rest_duration,total_duration,wait_time");
  //  Serial.println("   Example: 8.0,2.5,1.0,30,5");
  //  Serial.println("2) 1 value: degrees (positive=CCW, negative=CW)");
  //  Serial.println("   Example: 90");
}

char data[1];

void loop() {
  if (Serial.available()) {
    Serial.readBytes(data, 1);

    if (data[0]) {
      oneHalfStep(1);
    } else {
      oneHalfStep(-1);
    }
    //    line.trim();
    //
    //    if (line.length() == 0)
    //      return;
    //
    //    int tokenCount = countTokens(line);
    //
    //    if (tokenCount == 1) {
    //      float degrees = 0.0f;
    //      if (parseOneFloat(line, degrees)) {
    //        //        Serial.print("Rotate degrees command: ");
    //        //        Serial.println(degrees, 6);
    //        //        Serial.print("Using RPM: ");
    //        //        Serial.println(currentRpm, 6);
    //
    //        moveDegrees(degrees);
    //        //        Serial.println("Degree move complete. Ready for next
    //        //        command.");
    //      } else {
    //        //        Serial.println("Parse error (1-value mode). Example: 90
    //        or
    //        //        -180");
    //      }
    //    } else if (tokenCount == 5) {
    //      if (parseFiveFloats(line, rpmCmd, moveDurationS, restDurationS,
    //                          totalDurationS, waitTimeS)) {
    //        runProfile(rpmCmd, moveDurationS, restDurationS, totalDurationS,
    //                   waitTimeS);
    //        //        Serial.println("Ready for next command.");
    //      } else {
    //        //        Serial.println("Parse error (5-value mode).
    //        //        Example: 8.0,2.5,1.0,30,5");
    //      }
    //    } else {
    //      //      Serial.println("Invalid input count. Send either 1 value
    //      (degrees)
    //      //      or 5 values.");
    //    }
  }
}
