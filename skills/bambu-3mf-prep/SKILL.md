---
name: bambu-3mf-prep
description: >-
  Validates, cleans, and prepares downloaded 3MF files (e.g., from MakerWorld, Printables, or Meshy)
  for the user's Bambu Lab X2D dual-nozzle printer. Re-targets machine, build plate, process, and
  filament presets to X2D, maps colors against the user's filament inventory, suggests optimal
  AMS + Auxiliary Nozzle grouping, sanitizes non-ASCII object names, and runs headless Bambu Studio
  CLI slicing to surface floating regions, warnings, print time, and filament usage. Use whenever
  the user downloads a new 3MF, asks to prep/check/validate a 3MF for printing, or wants to update
  their filament inventory or printer profile.
---

# Bambu Lab X2D 3MF Preparation & Validation Skill

Use this skill whenever the user has a `.3mf` file to prepare for their **Bambu Lab X2D** or wants to manage their printer profile and filament inventory.

## Configuration Files

- Printer hardware & defaults: [printer_profile.json](./config/printer_profile.json)
- Filament spool inventory & currently loaded AMS/Aux slots: [filament_inventory.json](./config/filament_inventory.json)
- Main automation script: [prep_3mf.py](./scripts/prep_3mf.py)

## Workflow: Preparing a Downloaded 3MF

1. **Run the prep & validation script** on the downloaded `.3mf` file (original file is never modified; outputs `<stem>_x2d_ready.3mf` alongside it):
   ```bash
   python3 .agents/skills/bambu-3mf-prep/scripts/prep_3mf.py prep "<path/to/file.3mf>"
   ```
   Optional flags:
   - `--process "<preset>"` — override process preset (e.g. `"0.12mm High Quality @BBL X2D"`, `"0.20mm Standard @BBL X2D"`). By default, maps the source file's layer height to the matching X2D preset.
   - `--bed "<plate>"` — override build plate (default: `Textured PEI Plate`).
   - `--support keep|on|critical|off` — keep author's support setting (default), force supports on/off, or use `critical` (`tree(auto)` + `on build plate only` + `critical regions only`) to fix floating-region warnings on articulated models without fusing joints.
   - `--use-inventory-colors` — replace 3MF hex colors with exact hex codes from `filament_inventory.json`.
   - `--no-slice` — skip headless Bambu Studio slice validation for a fast dry-run.

2. **Review the report with the user**:
   - **Mismatches fixed**: Confirm source printer/plate/filament presets were converted to `Bambu Lab X2D 0.4 nozzle` + `Textured PEI Plate`, and any non-ASCII object names (which show as `???` in Bambu Studio) were sanitized.
   - **Filament inventory & dual-nozzle grouping**:
     - Show which spools from `filament_inventory.json` match each color in the 3MF and whether they are currently loaded in `ams_1..4` or `aux_nozzle`.
     - Highlight any color that is **missing** or **unconfirmed** in the inventory.
     - Present the **X2D Dual-Nozzle Grouping** recommendation: place the single heaviest-usage color on the **Auxiliary Nozzle** (no AMS) and the remaining ≤4 colors on the **Main Nozzle AMS** to minimize filament swaps and purge waste.
   - **Slicer warnings (especially floating regions)**:
     - If `floating regions` is reported, check whether the model is an **articulated / print-in-place flexi** model.
       - For articulated print-in-place models, **do NOT blindly enable global auto-supports**, because supports inside joints can fuse the links. Instead, advise the user to inspect the flagged regions in Bambu Studio Preview (layer slider / overhang view) and use **Paint-on Supports** only on true non-joint overhangs (like shoe cuffs or leaf tips), or verify if the author intentionally designed them to bridge without supports.
       - For non-articulated figurines/statues, recommend enabling `tree(auto)` supports (`--support on`) and re-running the slice check.

3. **Handoff to Bambu Studio**:
   - Link the generated `*_x2d_ready.3mf` file so the user can open it directly in Bambu Studio, visually inspect, slice, and print.

## Workflow: Updating Filament Inventory or Printer Settings

- To view current inventory:
  ```bash
  python3 .agents/skills/bambu-3mf-prep/scripts/prep_3mf.py inventory list
  ```
- When the user tells you what spools they own, what they bought, or what is currently loaded in `ams_1..4` and `aux_nozzle`, edit [filament_inventory.json](./config/filament_inventory.json) directly:
  - Set `"status": "owned"` for confirmed spools.
  - Set `"loaded"` keys (`ams_1`, `ams_2`, `ams_3`, `ams_4`, `aux_nozzle`) to the matching spool `id` (or `null` if empty).
  - Match `"preset"` to an exact filename (without `.json`) from `/Applications/BambuStudio.app/Contents/Resources/profiles/BBL/filament/` (e.g., `Bambu PLA Basic @BBL X2D 0.4 nozzle`, `Bambu PLA Matte @BBL X2D 0.4 nozzle`, `Bambu PLA Silk+ @BBL X2D 0.4 nozzle`, `Generic PLA @BBL X2D 0.4 nozzle`).
- If the user changes nozzles (e.g. 0.2mm / 0.6mm) or default build plate, update [printer_profile.json](./config/printer_profile.json).
