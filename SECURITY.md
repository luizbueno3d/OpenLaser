# Security and Safety

OpenMopa controls hardware that can emit laser radiation. Treat this project as experimental software and use it only with appropriate laser safety controls, enclosure, interlocks, eyewear, ventilation, and trained operators.

## Sensitive files not meant for git

Do not commit local machine or laser-profile files, including:

- `markcfg*`
- correction/calibration files such as `*.cor`
- job/design files such as `*.ezd`, `*.dxf`, `*.svg`, `*.stl`
- `layer_settings.json`
- local virtual environments and logs

The repository `.gitignore` excludes these by default.

## Reporting issues

Please open a GitHub issue for safety bugs, crashes, or unexpected emission behavior. Do not include private calibration files, serial numbers, or proprietary job files in public reports.
