# Model 1.4 XLSX reconciliation

Runtime 0.5.1 / OPC UA model 1.4.0 was reconciled against the 14 August 2026
Kria DCS intake workbook, draft 0.10:

```text
GIZMo_Kria_DCS_Intake.xlsx
SHA-256 71107acaf6bafab851ed0c628efe72339addc2207c8a4df8191d40b99a09491a
```

The earlier model 1.3.1 contract lacked 18 selected workbook entries. Model
1.4.0 adds all of them without changing an existing NodeId or datatype:

- `Measurement.StimulusCurrentRmsAmpere`;
- `Operations.RestartMeasurementEngine`, `AbortCalibration`, and
  `RestoreNormalState`;
- the nine `Operations.CommandGateState` / `LastCommand*` gate and audit
  readbacks; and
- the five `Calibration.OperationState`, `ProgressPercent`,
  `LastOperationTime`, `LastOperationResult`, and `RestorationState`
  readbacks.

The resulting generated contract contains 43 objects, 472 variables, and
eight methods. Its canonical content digest is:

```text
7dcad3112b8d10adf9b05c6b7acc41b1d52dd4ec97b15c94baabd6cc0850bf49
```

All 87 selected Kria workbook rows are present in the generated contract with
matching node class, OPC UA datatype, effective access class, and engineering
unit. Reproduce the comparison from the repository root with:

```sh
uv run --with openpyxl python tools/validate-dcs-intake.py \
  /path/to/GIZMo_Kria_DCS_Intake.xlsx
```

Presence is not a claim of commissioned physical capability. Stimulus current
is deliberately `NaN`/`BadNotSupported` until the monitor chain is validated.
Mutations are authenticated, role-gated, serialized, and audited, but the
package default is `CommandGateState=Disabled`. Calibration progress is
`BadDataUnavailable` when the recovered engine cannot report a defensible
percentage. A method acceptance result is never treated as physical success;
restart and restoration require independent process/hash/fresh-measurement
readback.
