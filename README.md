# demo_muon_chamber

Python app to show the effect of the high voltage on muon chamber wires.

Built for an outreach demonstration with an LHCb muon chamber: a **CAEN DT5471P**
high-voltage power supply (+3 kV / 500 µA) ramps the wires while a **USB endoscope
camera** shows what happens inside the chamber. Both are driven from one window.

## Hardware

| | |
|---|---|
| HV power supply | CAEN DT5471P, USB, single channel, +3 kV / 500 µA |
| Camera | USB endoscope (UVC), with its own LED ring |
| Host | macOS (developed on Apple Silicon); the code is portable, see *Portability* |

The DT547x is **not** supported by the CAEN HV Wrapper library, so this project
talks to it directly over its USB serial port, using the ASCII protocol
documented in CAEN user manual **UM3178, chapter 4**. No CAEN library or driver
is needed.

## Requirements

### System

- **Python 3.10+** with **tkinter**. tkinter is not a pip package. Homebrew's
  Python ships without it:
  ```bash
  brew install python-tk@3.13
  ```
- **macOS camera permission.** The first run will ask; if it does not, enable it
  manually under *System Settings → Privacy & Security → Camera* for the app you
  launch from (Terminal, or Visual Studio Code).

### Python packages

```bash
python3 -m venv ~/.venv/hep2
~/.venv/hep2/bin/python3 -m pip install -r requirements.txt
```

Run everything with that interpreter. A plain `python3` will fail with
`ModuleNotFoundError`:

```bash
~/.venv/hep2/bin/python3 console_lhcb.py
```

## Quick start

```bash
# the demo console: video + HV in one window
~/.venv/hep2/bin/python3 console_lhcb.py

# without any hardware, to rehearse the interface
~/.venv/hep2/bin/python3 console_lhcb.py --sim
```

The console finds the endoscope and the power supply by itself. Press **F** for
fullscreen on the projector.

## Programs

| File | What it is |
|---|---|
| `console_lhcb.py` | **The demo console.** Video + HV in one window, LHCb logo, fullscreen. Uses the endoscope only. |
| `gui_caen.py` | Standalone HV control panel. |
| `video_camera.py` | Standalone video viewer, with a camera picker and recording. |
| `controllo_hv.py` | Command-line HV control. `--porte`, `--info`, `--vset`, `--sim`. |
| `hv_backend.py` | HV driver (DT547x serial protocol, CAEN HV Wrapper, simulator). No GUI. |
| `camera_backend.py` | Camera capture on a background thread. No GUI. |

The standalone programs still work; the console does not replace them.

Snapshots and recordings are written to `acquisizioni/`.

### Command line examples

```bash
~/.venv/hep2/bin/python3 controllo_hv.py --porte          # list serial ports
~/.venv/hep2/bin/python3 controllo_hv.py --info           # identify, read state
~/.venv/hep2/bin/python3 controllo_hv.py --vset 1500      # ramp to 1500 V
~/.venv/hep2/bin/python3 controllo_hv.py --sim --vset 500 # no hardware
```

`--info` and a bare read never switch the channel on or off. The ramp commands
switch it off again on exit unless you pass `--lascia-acceso`.

### Keyboard shortcuts

| Key | Action |
|---|---|
| `F` / `F11` | Fullscreen |
| `Esc` | Leave fullscreen |
| `S` | Snapshot |
| `Space` | Start/stop recording (`video_camera.py` only) |

## Troubleshooting

**The HV will not switch on.** Check the enable switch first, not the software.
The channel refuses `SET:ON` while it is disabled, and the console shows `DIS`
in the status line. There is a switch on the back of the box as well as the
front-panel EN/DIS toggle — the front one is lockable, pull the actuator out
before moving it.

**`VMon` never reaches `VSet`.** Most likely the current limit. Check *Corrente
max* (ISet): if the load draws more, the channel becomes a current generator and
sits below the setpoint. With Trip at 1000 s (= infinite) it will not trip, it
will just never arrive.

**You cannot exceed a certain voltage.** `MaxV` is set by a hardware trimmer on
the front panel and overrides anything sent over USB.

**"collegamento perso" / `Device not configured`.** The module re-enumerates on
the USB bus when the enable switch is flipped. The console detects this, rescans
the ports and reconnects by itself within a couple of seconds. If it persists,
the module is really gone — check the cable.

**Black video.** The endoscope's LED ring is controlled by a wheel on the cable,
not over USB; software cannot change it. The viewer warns when every pixel is
zero.

**Video runs at ~10 fps.** That is auto-exposure, not a bug: the sensor
integrates for 1/10 s in dim light. More light and it climbs towards its rated
25 fps.

**The camera picker shows the wrong camera.** See below — do not trust the names.

## Notes for whoever maintains this

Two things in this setup are actively misleading, and both are commented in the
code:

- **The DT54xx manual contradicts itself about the status word.** The protocol
  table in chapter 4 and the LabVIEW *Channel Status* VI disagree from bit 8
  onwards. The LabVIEW ordering is the correct one — verified against the
  hardware — so **bit 11 is DISABLED (the enable switch), not KILL**.
- **Camera names do not match OpenCV indices**, and the AVFoundation order,
  the `system_profiler` order and the OpenCV index order are three different
  orders that also change between runs. Never select a camera by name. The
  console identifies the endoscope by requesting 1600×1200, a mode only it
  accepts; `video_camera.py` probes each index and offers an *Auto* mode that
  picks the first one returning a non-black frame.

The serial port name changes between plugs (`usbmodem21301` → `usbmodem21201`);
all the programs pick the port automatically, so this does not matter.

`.lhcb-logo-cache.png` is a downscaled copy of the logo, generated on first run
because the original is 11825×7100 and cost ~5 s per launch. It is regenerated
automatically and does not need to be committed.

## Portability

The HV side is pure pyserial and works anywhere. The camera side uses OpenCV,
which is cross-platform, but the AVFoundation backend selection and the
camera-identification workarounds in `camera_backend.py` and `console_lhcb.py`
are macOS-specific and would need revisiting on Linux.

## Safety

This equipment produces up to 3 kV at a potentially lethal current. Never
connect or disconnect the SHV cable with the channel enabled; set the switch to
DISABLE and wait at least 30 s first. Earth the unit before connecting the load.
See the safety section of UM3178.
