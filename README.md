# UV Lamp Controller

Desktop BLE controller and Arduino firmware for a UV lamp with thermocouple
feedback. The app connects to a BLE device named `ThermoCouple`, starts an
onboard recipe, plots live temperature, and saves CSV logs to `data/`.

The Arduino owns active recipes. Once a recipe is started, the board keeps the
timer, temperature-band control, relay dwell, and UV-on counter running even if
the laptop sleeps, the BLE link drops, or the app is closed.

![UV Lamp Controller GUI](docs/gui.png)

## Requirements

- Windows 10/11 or macOS with Bluetooth LE enabled
- Python 3.11 or newer
- A BLE peripheral advertising as `ThermoCouple`

On macOS, allow Bluetooth access for Terminal, your Python app, or your IDE if
prompted by System Settings.

## Install

Clone the repo:

```powershell
git clone https://github.com/itschendai/uv-lamp-controller.git
cd uv-lamp-controller
```

Create and activate a virtual environment.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe uv_lamp_controller.py
```

macOS:

```bash
.venv/bin/python uv_lamp_controller.py
```

Click `Connect`, then use `Start` to begin temperature control and logging.
Each run creates `data/uv_lamp_log_YYYYMMDD_HHMMSS.csv`.

## GUI User Manual

1. Connect to the device

   Click `Connect` in the BLE Connection panel. The app scans for a BLE device
   named `ThermoCouple`. When connected, the button changes to `Disconnect`.

2. Set the temperature band

   Enter the lower and upper temperature limits in Celsius. During a run, the
   controller turns the lamp ON at or below the lower limit and OFF at or above
   the upper limit. The plot shades the active band and draws both limit lines.
   Place the thermocouple as close as practical to the measurement target so the
   controller responds to the temperature you actually care about.

3. Choose the run timer

   Set hours, minutes, and seconds. `Total time` counts wall-clock run time.
   `UV time` counts only while the lamp command is ON.

4. Start, stop, or reset a run

   `Start` uploads a new recipe to the Arduino, clears the live table and plot,
   creates a CSV log, and enters warm-up control on the board. `Stop` sends
   `RECIPE_STOP`, turns the lamp OFF, and closes the active log after the
   Arduino reports that it stopped. If the laptop disconnects during a run, the
   recipe continues on the Arduino; reconnect to monitor or stop it.
   Disconnecting or closing the app does not send a lamp command, and closing
   the app during a run detaches from the recipe instead of stopping it.

5. Use manual lamp control

   When connected and not running a recipe, `Lamp ON` and `Lamp OFF` send direct
   commands. Manual buttons are disabled during a run so automatic temperature
   control owns the lamp state.

6. Monitor live data

   The top metric cards show current thermocouple temperature, chip
   temperature, lamp command, UV-on time, and remaining goal time. The chart and
   table update from BLE notifications. CSV `elapsed_s` uses the device
   timestamp from each thermocouple sample, not PC receive time.

7. Find saved data

   Logs are saved under `data/` as `uv_lamp_log_YYYYMMDD_HHMMSS.csv`.

## BLE Contract

The app scans for either the local name `ThermoCouple` or this service UUID:

| Item | Value |
| --- | --- |
| Service UUID | `7f3fd100-9a7e-4f4f-a5f1-f6c5437fd801` |
| Data characteristic | `7f3fd101-9a7e-4f4f-a5f1-f6c5437fd801` (`READ`, `NOTIFY`) |
| Command characteristic | `7f3fd102-9a7e-4f4f-a5f1-f6c5437fd801` (`WRITE`) |

Live data notifications use one line per sample:

```text
DATA,arduino_ms,thermocouple_C,internal_C,sensor_ok,fault_bits,raw,lamp,uv_on_ms
```

The Arduino also keeps a RAM-backed ring buffer of the most recent 600 recipe
samples, about 10 minutes at the default 1 Hz sample rate. On reconnect, the app
requests missed rows with `HISTORY_SINCE,last_arduino_ms`; replayed rows use the
same sample fields with a `HIST` prefix. If the laptop is disconnected longer
than the ring buffer covers, `HISTORY_BEGIN` reports `lost=1`.

The app sends:

```text
RECIPE_START,lower_C,upper_C,duration_s,TOTAL
RECIPE_START,lower_C,upper_C,duration_s,UV
RECIPE_STOP
HISTORY_SINCE,arduino_ms
LAMP_ON
LAMP_OFF
STATUS
```

Manual `LAMP_ON` and `LAMP_OFF` are accepted only while no recipe is running.
The desktop sends BLE commands one at a time and waits for the Arduino response
before sending the next command. During a recipe, relay dwell can delay lamp ON,
but lamp OFF is always applied immediately.
`STATUS` returns a key/value line that lets the app reattach to an existing
recipe:

```text
STATUS,relay_pin=7,lamp=ON,last_lamp_ms=12340,last_lamp_reason=LOWER,ble=CONNECTED,recipe=RUNNING,last=NONE,mode=TOTAL,lower=26.00,upper=30.00,duration_s=1800,elapsed_s=120,uv_on_s=82,remaining_s=1680,start_ms=12345,startup=0,history_count=120,history_capacity=600
```

`arduino_ms` should be captured when the thermocouple is read. The plot and CSV
`elapsed_s` use that device timestamp, so BLE notification delay does not shift
individual datapoints.
