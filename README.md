# OpenLaser

<p align="center">
  <img src="assets/icons/openmopa.png" alt="OpenLaser icon" width="160">
</p>

OpenLaser is an open-source workspace for preparing jobs for:

- JPT MOPA fiber galvo lasers using JCZ/BJJCZ LMC controllers.
- GRBL-compatible CO₂ laser cutters.
- GRBL-compatible diode lasers.
- GRBL-compatible pen plotters.

You can import artwork, arrange it on the machine bed, assign operations and
settings to layers, preview the job, and either run it directly or export
G-code.

> [!IMPORTANT]
> OpenLaser is early-stage software that controls equipment capable of causing
> fires, eye injuries, burns, fumes, and machine damage. Never leave a running
> machine unattended. Wear the correct laser safety protection, use ventilation,
> keep a suitable fire extinguisher nearby, and test at low power first.

## What works today

| Machine | What OpenLaser does | How the job reaches the machine |
| --- | --- | --- |
| JPT M7 MOPA, 200 mm galvo | Import, plan, red-light frame, and mark | Direct JCZ/LMC USB control |
| GRBL CO₂, 600 × 400 mm | Import, plan, raster/vector processing, and G-code export | Open the exported file in your trusted GRBL sender |
| GRBL diode, 400 × 400 mm | Import, plan, raster/vector processing, and G-code export | Open the exported file in your trusted GRBL sender |
| GRBL pen plotter, A4 | Import, plan, and pen-motion G-code export | Open the exported file in your trusted GRBL sender |

The GRBL CO₂ profile is for controllers that accept normal GRBL G-code. It does
not control Ruida DSP controllers yet.

## Install OpenLaser on a Mac — easiest method

You do not need to know programming or Terminal commands for this method.

### 1. Download OpenLaser

1. Open the [OpenLaser GitHub page](https://github.com/luizbueno3d/OpenLaser).
2. Click the green **Code** button.
3. Click **Download ZIP**.
4. Open your Downloads folder.
5. Double-click the ZIP file to unzip it.
6. Move the new `OpenLaser` folder to a permanent place such as `Documents`.

Do not run OpenLaser directly from the Downloads folder if you intend to move
it later. The generated app stays connected to the project folder beside it.

### 2. Make sure Python is installed

OpenLaser needs Python 3.10 or newer.

1. Open [python.org/downloads](https://www.python.org/downloads/).
2. Download the current Python 3 installer for macOS.
3. Open the downloaded installer.
4. Follow the normal installation steps.

If you already have Python 3.10 or newer, you may skip this step. The OpenLaser
installer checks for it automatically.

### 3. Run the OpenLaser installer

1. Open the `OpenLaser` folder.
2. Find **Install OpenLaser.command**.
3. Right-click it, or hold Control and click it.
4. Choose **Open**.
5. If macOS shows another warning, choose **Open** again.
6. Wait while the installer creates OpenLaser's private environment and installs
   its dependencies.
7. When you see **Done! OpenLaser is installed**, press any key to close the
   installer window.

The installer creates `OpenLaser.app` inside the same folder.

### 4. Start OpenLaser

1. Double-click **OpenLaser.app**.
2. A Terminal window opens. Leave it open while you use OpenLaser.
3. Your web browser opens to `http://127.0.0.1:8765`.

This address is local to your computer. The interface is displayed in a browser,
but the job is not uploaded to a website.

You may drag `OpenLaser.app` to your Dock for easier access. Keep the actual app
file inside the OpenLaser project folder.

### 5. Stop OpenLaser

Close the Terminal window that OpenLaser opened, or click that window and press
Control-C. Closing only the browser tab does not stop the OpenLaser server.

## Install with Terminal — developers and advanced users

Open Terminal and run these commands one line at a time:

```bash
git clone https://github.com/luizbueno3d/OpenLaser.git
cd OpenLaser
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[hardware]"
openlaser ui
```

Then open `http://127.0.0.1:8765` if the browser does not open automatically.

Next time, start it with:

```bash
cd OpenLaser
source .venv/bin/activate
openlaser ui
```

## How to use OpenLaser — your first job

The normal workflow is:

**Choose a machine → import artwork → position it → choose layers → enter
settings → plan → frame/verify → run or export.**

### Step 1: Choose your machine

Use the **Machine** menu at the top of the screen.

- Choose **JPT M7 MOPA · 200 mm galvo** for the original OpenMopa fiber setup.
- Choose **CO₂ cutter · GRBL 600 × 400 mm** for a GRBL CO₂ machine.
- Choose **Diode laser · GRBL 400 × 400 mm** for a GRBL diode machine.
- Choose **Pen plotter · GRBL A4** for the built-in A4 plotter profile.

The canvas changes to the selected machine's work area. Choosing the correct
machine is important because OpenLaser rejects artwork outside that area.

### Step 2: Import your artwork

1. Find **Import** in the left panel.
2. Click **Choose File**.
3. Select an `.svg`, `.dxf`, or `.stl` file.
4. Wait for the artwork to appear on the canvas.
5. Click **Fit** if the artwork is off-screen or very small.

DXF is the most complete CAD import path. Basic SVG line commands are supported,
but SVG Bezier curves and arc commands are not flattened yet. If a complicated
SVG imports incorrectly, convert its curves to polylines in your design program
or export DXF.

### Step 3: Select and position the artwork

- Click an object to select it.
- Shift-click to add or remove objects from the selection.
- Drag an empty area to draw a selection window.
- Double-click a line to select its connected shape.
- Use **X mm** and **Y mm** to position selected artwork.
- Use **Scale** to resize it.
- Use **Rotate deg** to rotate it.
- Click **Center Selection** to put the selected artwork at the bed center.
- Press Delete or Backspace to remove selected artwork.
- Use the mouse wheel to zoom.
- Hold Space and drag to pan.
- Press `f` to fit the artwork on screen.
- Press `0` to return to the full machine view.
- Use Command-Z to undo and Shift-Command-Z to redo on macOS.

The design coordinate origin is the center of the displayed work area.
OpenLaser converts it to the positive coordinates used by GRBL during export.

### Step 4: Put artwork on a layer

Layers tell OpenLaser what to do with each object.

1. Select one or more objects.
2. Click the desired row in **Cuts / Layers**.
3. Click the arrow button marked **→**.

The default layers are:

| Layer | Meaning |
| --- | --- |
| Vector Engrave | Follow the imported lines with the laser/tool |
| Vector Cut / Deep Mark | Follow the imported lines, normally more slowly or with more passes |
| Raster | Fill closed shapes with closely spaced scan lines |
| Frame Only | Include the shape while framing, but do not output it while marking |

You can rename layers, change their color, reorder them, hide them, or disable
their output.

### Step 5: Choose the layer settings

Click a layer to edit its settings.

- **Mode:** vector engrave, vector cut, raster engrave, frame only, or disabled.
- **Speed mm/s:** how fast the tool moves while working.
- **Power %:** laser output from 0 to 100 percent.
- **Passes:** how many times to repeat the path.
- **Frequency kHz:** MOPA pulse frequency. Only shown for compatible machines.
- **Pulse ns:** MOPA pulse width. Only shown for compatible machines.
- **Pitch mm:** distance between raster fill lines. Smaller values produce more
  lines and a longer job.
- **Hatch angle:** direction of raster fill lines.
- **Angle step/pass:** rotation added to the hatch angle on each raster pass.
- **Air assist:** emits `M8`/`M9` for compatible GRBL laser profiles.

There is no universal safe power or speed. Correct values depend on the exact
laser, material, focus, lens, cooling, air assist, and desired result. Start
with a small test piece and settings recommended by your machine or material
supplier. Do not copy MOPA settings into a CO₂ or diode machine.

For a pen plotter, laser power, frequency, and pulse width are hidden because
the pen does not use them.

### Step 6: Plan the job

Click **Plan Job** before every run or export.

Planning checks:

- Whether there is usable geometry.
- Which layers will produce output.
- The job's bounding box.
- Layer settings.
- Whether every point fits inside the selected machine.
- MOPA emission safety requirements when using direct control.

Read the **Activity** and **Job Summary** panels. Fix every reported error before
continuing.

### Step 7A: Run a MOPA job directly

This path is only for the built-in JCZ/LMC JPT M7 profile.

1. Make sure the machine, enclosure, extraction, and safety equipment are ready.
2. Connect USB and power on the machine.
3. Click **Frame Once** to trace the job with the red aiming light.
4. Confirm the frame is entirely inside the fixture/material.
5. Recheck every layer's power, frequency, pulse width, speed, and passes.
6. Type `ARM` exactly in the ARM box.
7. Click **Mark Job**.
8. Read the final confirmation dialog.
9. Confirm only when the area is safe and you are ready for emission.
10. Stay with the machine until the job finishes.

Use **Start Continuous Frame** only while positioning. Click **Stop Frame** when
finished. Use the red **STOP ENGRAVING** button if the job must stop immediately.

### Step 7B: Export a CO₂ or diode laser job

OpenLaser currently creates the job file; your normal GRBL sender communicates
with the machine.

1. Click **Plan Job** and correct every error.
2. Click **Export G-code**.
3. Save the `.gcode` file somewhere easy to find.
4. Open your trusted GRBL sender.
5. Connect the sender to the correct machine and serial port.
6. Load the exported `.gcode` file.
7. Confirm units are millimetres and the job fits the machine bed.
8. Use the sender's frame/boundary feature with the laser safely disabled or at
   its approved framing power.
9. Recheck origin, focus, material, ventilation, cooling, and air assist.
10. Start the job from the sender and remain with the machine.

The CO₂ profile uses constant-power `M3`. The diode profile uses dynamic-power
`M4`. Laser power is scaled to the built-in GRBL range of `S0` through `S1000`.
Confirm that this matches your controller's `$30` configuration before use.

### Step 7C: Export a pen plotter job

1. Select **Pen plotter · GRBL A4**.
2. Import and position the artwork.
3. Assign it to an output layer and set speed/passes.
4. Click **Plan Job**.
5. Click **Export G-code**.
6. Load the file in your GRBL sender.
7. Test pen-up and pen-down commands with the pen away from the paper.
8. Frame or dry-run the movement.
9. Install the pen and start the job.

The built-in plotter profile uses `M3 S1000` for pen down and `M5` for pen up.
Plotter firmware and servo arrangements vary. Verify those commands on your
machine before allowing the pen to touch the paper.

## Raster engraving explained simply

Vector modes follow the outlines in your file. Raster mode fills the inside of
a closed shape with parallel lines.

For raster fill to work:

1. The shape must be closed; its final point must return to its first point.
2. The Raster layer must have **Output** enabled.
3. Pitch must be greater than zero.

Open raster paths are skipped and reported. A smaller pitch creates more fill
lines, higher density, and a longer job.

## Selected-only jobs, mirroring, and repeats

- Enable **Selected only** to plan, frame, mark, or export only selected objects.
- Enable horizontal or vertical mirroring when the job or material requires it.
- Use **Repeat** to repeat the full job. Be extremely careful with repeat counts
  on lasers because heat accumulates between repetitions.

## Calibration for the MOPA galvo

The Advanced panel includes **Scale correction** for the direct MOPA profile.

1. Mark a square of a known intended size.
2. Measure the actual marked square accurately.
3. Calculate `measured size ÷ intended size`.
4. Enter that number as Scale correction.

Example: a 50 mm square measures 59.17 mm. Enter `1.1834`.

Calibration is stored locally in `calibration.json`. OpenLaser does not modify
your original JCZ correction or mark configuration files.

## Where OpenLaser saves your work

OpenLaser keeps these files beside the project:

- `workspace.json`: imported objects and their transforms.
- `layer_settings.json`: layer names, modes, and parameters.
- `calibration.json`: local MOPA scale correction.

These machine-local files are ignored by Git. The workspace is saved
automatically after changes.

## Troubleshooting

### macOS says the installer cannot be opened

Right-click **Install OpenLaser.command**, choose **Open**, and then choose
**Open** again in the warning dialog. Double-clicking alone may be blocked by
Gatekeeper the first time.

### The installer says Python is missing

Install Python from [python.org/downloads](https://www.python.org/downloads/),
then run **Install OpenLaser.command** again.

### The browser did not open

Leave the OpenLaser Terminal window running and manually open:

```text
http://127.0.0.1:8765
```

### The page says “Server connection lost”

The Terminal window running OpenLaser was probably closed. Start
`OpenLaser.app` again.

### `openlaser: command not found`

Activate the private environment first:

```bash
cd OpenLaser
source .venv/bin/activate
openlaser ui
```

### MOPA hardware is not detected

1. Check machine power.
2. Reconnect the USB cable.
3. Try another cable or USB adapter.
4. Activate the environment and run:

```bash
openlaser detect
```

Detection does not send laser commands.

### The JCZ/LMC controller connects but does not respond

The controller can become wedged if a process stops during a USB transfer.
Turn the complete laser machine off, wait at least 15 seconds, and turn it on
again. Unplugging only the USB cable is not enough because the controller is
powered by the machine.

### Export says geometry is outside the work area

OpenLaser intentionally refuses to clip the job. Select the correct machine,
then move or scale the artwork until it is fully inside the visible bed.

### Raster produces no lines

The source shape is probably open. Join its endpoints in your CAD program or
use **Join Connected Lines** with an appropriate tolerance.

### The plotter does not lift or lower the pen

Your controller may use different servo commands. The built-in profile expects
`M3 S1000` for down and `M5` for up. Test safely and customize the machine
profile in code before running a real drawing.

## Command-line tools

Activate `.venv` before using these commands.

```bash
openlaser machines
openlaser detect
openlaser show-config
openlaser pulse-widths
openlaser inspect-profile
openlaser plan-test-box --power 10 --frequency-khz 30 --pulse-width-ns 200
openlaser ui
```

`openmopa` remains available as a compatibility alias, but new documentation
uses `openlaser`.

## Test without hardware

Mock mode exercises the MOPA software chain without contacting a real board:

```bash
OPENMOPA_MOCK=1 openlaser ui
```

Results clearly report `"mock": true`.

## Supported platforms

- **macOS:** primary development and packaging platform.
- **Windows:** browser UI should be portable, but USB and packaging still need
  hardware validation.
- **Linux:** browser UI should be portable, but USB permissions and hardware
  paths still need validation.

Treat Windows and Linux machine control as experimental until tested with the
specific hardware.

## Safety rules enforced by OpenLaser

For direct MOPA emission:

- Power must be between 0 and 100 percent.
- Frequency must be inside the machine profile limits.
- Pulse width must match the machine profile's allowed table.
- At least one markable path must exist.
- The operator must type `ARM`.
- A final confirmation is required before emission.

For GRBL export:

- Coordinates must be finite.
- Every coordinate must fit inside the selected work area.
- Layer power must be between 0 and 100 percent.
- Layer speed must be greater than zero and is capped to the machine profile.
- The generated file begins in millimetres and absolute coordinate mode.
- Tool-off commands are inserted before travel and at the end of the job.

Software checks do not replace machine interlocks, enclosure switches,
ventilation, fire prevention, training, or operator attention.

## Current limitations

- Direct USB control currently targets the original JCZ/LMC MOPA setup only.
- GRBL CO₂, diode, and plotter jobs are exported rather than streamed directly.
- Ruida DSP controllers are not supported.
- Built-in gantry bed sizes and plotter commands are not yet editable in the UI.
- SVG import supports line-oriented `M`, `L`, `H`, `V`, and `Z` paths; complex
  curves should be flattened before import.
- Windows and Linux hardware paths need real-machine validation.

## Development

Run the complete test suite with:

```bash
source .venv/bin/activate
python -m unittest discover -v
```

Important modules:

```text
mopa_luiz/machines.py   built-in machine profiles and capabilities
mopa_luiz/gcode.py      GRBL laser and plotter G-code compiler
mopa_luiz/layers.py     layer model and operation grouping
mopa_luiz/raster.py     hatch-fill generation
mopa_luiz/safety.py     direct-emission safety policy
mopa_luiz/live.py       JCZ/LMC direct-control path
mopa_luiz/ui.py         local HTTP server and browser interface
```

The Python package still uses the internal `mopa_luiz` name for compatibility
with the original OpenMopa codebase.

## License and security

OpenLaser is released under the [MIT License](LICENSE).

Please read [SECURITY.md](SECURITY.md) before reporting a security issue.
