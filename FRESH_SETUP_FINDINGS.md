# Fresh-setup audit — findings

Branch-local bucket list for `feature/fresh-setup-audit`. **Delete before push** —
only the fixes belong in the PR.

Every item below was reproduced against a real first install: bootstrap-only
`config.yaml`, empty SQLite store, wizard driven in Chromium via
`tests/web/wizard_driver.py`. Status: `open` / `fixed`.

---

## C0 — The wizard cannot complete. At all. *(critical)*

**Status:** open

On a fresh install the wizard's final save is rejected with **HTTP 422** for every
persona tested (defaults, EVCC, OpenHAB, HA+Solcast). Nothing is written; the store
still holds only the seven keys the migration seeded. The user sees
"Save failed: 422" and there is no way forward from inside the wizard.

Two fields cause it, and **the user never sees either of them**:

| key | value sent | schema says | why it's sent |
|---|---|---|---|
| `data_source.url` | `""` | `pattern ^https?://.+` | seeded empty by `_create_data_source_batch` ([migration.py](src/config_web/migration.py)); hidden because `data_source.type` is `default` |
| `pv_autoscaling.sensor_entity_id` | `""` | `validation={"required": True}` ([schema.py:1482](src/config_web/schema.py#L1482)) | `getting_started` level in a section **no wizard step covers** |

Both are `getting_started`, so `_finish` includes them
([wizard.js:816-824](src/web/js/wizard.js#L816-L824)); `_loadData` pre-filled them with
defaults ([:164-168](src/web/js/wizard.js#L164-L168)); and `_validate_updates`
([api.py](src/config_web/api.py)) validates every key it is handed without regard to
`depends_on`.

**Verified fix:** filtering the payload to dependency-met fields turns all three
personas green — P1 and P3 return `200 {"success": true}`, and the phantom
`pv_forecast.*` keys disappear at the same time. This one change closes C0, C2.1,
C2.3, C2.4 and C2.5.

---

## C1 — The wizard shows nothing, or the wrong things

**1.1 · The Inverter step is blank.** *(fixed)*
Measured: **4 fields rendered, 0 visible**, and no empty state.
[schema.py:1592](src/config_web/schema.py#L1592) gives `inverter.type`
`depends_on={"data_source.type": ["homeassistant"]}`. `data_source.type` defaults to
`default`, so the select is hidden; `address`/`user`/`password` need
`fronius_gen24*`/`victron`, also hidden; every other inverter field is
`level="standard"`, which the wizard never renders. With `openhab` the step is blank
permanently. The dependency is a copy-paste — `inverter.charge_from_grid` correctly
keys off `inverter.type`. **Drop it.**

**1.2 · Step order is unsatisfiable.** *(open)*
Optimizer → EVCC → **Inverter → Data Source**
([wizard.js:26-98](src/web/js/wizard.js#L26-L98)). Even a genuine HA user has not
answered Data Source when Inverter is shown. Data Source must come first.

**1.3 · No empty state, and a hollow review section.** *(open)*
`_getStepFields` ([wizard.js:907](src/web/js/wizard.js#L907)) filters by section and
level but not `_isDependencyMet`, so the `length === 0` guard at
[:266](src/web/js/wizard.js#L266) never fires — the user gets a title, a description
and blank space. The review step has the same mismatch
([:458](src/web/js/wizard.js#L458)) and renders `Inverter` as a heading with **zero
rows** under it.

**1.4 · Solcast and Victron cannot be completed.** *(open)*
`_check_dependencies` requires `pv_forecast_source.resource_id`, but that field is
`level="standard"` — the wizard only renders `getting_started`. A Solcast user fills
in the API key, reaches Finish, and gets `unmet_dependencies` naming a field the
wizard never offered. Reproduced: P5 returns `200 {"success": false}` with
`"Solcast selected as PV source but Resource ID/Installation ID is not configured"`.

**1.5 · Dead code in the live re-render.** *(open, latent)*
[wizard.js:604](src/web/js/wizard.js#L604) reads `this.currentStepIndex`; the property
is `this.currentStep` ([:18](src/web/js/wizard.js#L18)). The branch also calls
`this._attachFieldListeners()`, which **is not defined in the file**. Not currently
user-visible — the generic `_updateConditionalFields()` already hides and shows the
installation fields correctly on a source switch (verified: switching to `solcast`
leaves `source` + `api_key` visible). It is a latent throw, so remove or implement it.

**1.6 · "Invalid format" is not an error message.** *(open, minor)*
An empty `data_source.url` correctly blocks Next, but says only `Invalid format`
([wizard.js:792](src/web/js/wizard.js#L792)). Say what is wanted.

---

## C2 — The wizard writes data the user never entered

**2.1 · The payload is the whole schema, not the step.** *(open — closed by C0 fix)*
`_finish` posts every `getting_started` field, including `pv_autoscaling.*` and
`time_zone`, which no step displays.

**2.2 · Bootstrap keys land in the database.** *(open)*
`time_zone` is a `BOOTSTRAP_KEY` ([schema.py:15](src/config_web/schema.py#L15)).
[merger.py:68-75](src/config_web/merger.py#L68-L75) always prefers the bootstrap value,
so the stored copy is dead — but it carries `restart_required`, so **every** wizard
finish arms the restart banner regardless of what actually changed. Verified:
`time_zone in store == True` after every save.

**2.3 · Phantom PV installation.** *(open — closed by C0 fix)*
`_finish` re-indexes `pv_forecast.X` → `pv_forecast.0.X` only for location-based
sources ([:826-839](src/web/js/wizard.js#L826-L839)). For evcc/solcast/victron/
timeseries the unindexed keys go through as-is and
[merger.py:167-184](src/config_web/merger.py#L167-L184) synthesizes a bogus
installation at 47.5/8.5. (`config.js:124` deliberately skips these template keys; the
wizard does not.)

**2.4 · Render and save disagree.** *(open — closed by C0 fix)*
The location-source list at [wizard.js:920](src/web/js/wizard.js#L920) includes
`"default"`; the one at [:827](src/web/js/wizard.js#L827) does not.

**2.5 · Placeholder secrets persisted verbatim.** *(open — closed by C0 fix)*
`price.token = "tibberBearerToken"` and `inverter.password = "abc123"` are written even
when the field was never shown.

---

## C3 — Failures reported as success

**3.1 · "Setup Complete!" for a refused save.** *(open)*
`PUT /api/config/` returns **HTTP 200** with `{"success": false,
"unmet_dependencies": [...]}`, but `_finish` only checks `saveRes.ok`
([wizard.js:847](src/web/js/wizard.js#L847)).

**3.2 · A refused save is really a partial write.** *(open)*
Values are stored before the dependency check runs. Verified: a PUT rejected with
`success: false` still left `battery.capacity_wh = 99999` and
`pv_forecast_source.source = "solcast"` in the database. The docstring claims
"no save" — it is wrong.

**3.3 · `required` is enforced by the API but not the wizard.** *(open, minor)*
`_validateField` ([wizard.js:771](src/web/js/wizard.js#L771)) checks choices, min, max
and pattern, never `validation.required`. In practice pattern-backed fields still
block (verified), so the only field affected is the one from C0 — but the asymmetry is
what let C0 through. An empty HA `access_token` also carries no `*` and passes.

**3.4 · `GET /api/config/export` returns unmasked secrets.** *(open)*
It is what the wizard loads ([wizard.js:143](src/web/js/wizard.js#L143)), while
`GET /api/config/` masks them.

---

## C4 — Healthy config, degraded startup

**4.1 · Every PV source degrades on a fresh install.** *(fixed)*
Verified — with `pv_forecast: []`, `configuration_valid` is `False` and
`configuration_state` is `incomplete` for **all six** sources:

```
akkudoktor  False/incomplete    evcc       False/incomplete
solcast     False/incomplete    victron    False/incomplete
timeseries  False/incomplete    default    False/incomplete
```

Only the four location-based sources should care.
`PvInterface.__check_config` raises on the empty list
([pv_interface.py:272-276](src/interfaces/pv_interface.py#L272-L276)) *before*
dispatching on source, duplicating `__validate_pv_source_requirements`
([:387-400](src/interfaces/pv_interface.py#L387-L400)), which already scopes the check
correctly and has an explicit `elif source == "evcc"` no-op.

**4.2 · Hot-reload into evcc is refused.** *(fixed)*
The same check runs with `strict=True` ([:206](src/interfaces/pv_interface.py#L206))
and rolls back — a user switching to evcc from the UI cannot fix it there.

**4.3 · The error points at a section that does not exist.** *(fixed)*
"Settings ▸ PV Forecast"; `SECTION_META` has "PV Source" and "PV Installations".
Stale at [:113](src/interfaces/pv_interface.py#L113),
[:262](src/interfaces/pv_interface.py#L262),
[:2825](src/interfaces/pv_interface.py#L2825).

---

## C5 — Duplicated source of truth

**5.1 · `LOCATION_BASED_PV_SOURCES` exists five times.** *(open)*
[schema.py:44](src/config_web/schema.py#L44) is the intended SPOT; copies live in
`api.py`, [config.js:694](src/web/js/config.js#L694) and twice in `wizard.js`, with the
divergence in C2.4. The schema is already served to the frontend — same treatment
`BOOTSTRAP_KEYS` and `SECTION_META` got in `c124db6`.

**5.2 · EVCC placeholder asymmetry.** *(open)*
Verified against the API with `evcc.url` left at `http://yourEVCCserver:7070`:

```
pv_forecast_source.source=evcc  ->  blocked: False   <-- accepts the placeholder
inverter.type=evcc              ->  blocked: True
price.source=evcc               ->  blocked: True
```

---

## C6 — Test & CI gaps

**6.1 · `tests/web/` never runs in CI.** *(open)* No browser, no `playwright install`
step; `collect_ignore_glob` skips it silently, so the browser suite guards nothing.

**6.2 · The documented test command is broken.** *(open)* `pytest tests/` fails
collection with **14 errors**; only `python -m pytest tests/` works. CONTRIBUTING.md
and the developer docs tell contributors to run the broken form. `pytest.ini` is in
`.gitignore`.

**6.3 · Test tooling is declared nowhere machine-readable.** *(open)*
`pytest` / `pylint` / `black` / `playwright` exist only as ad-hoc `pip install` lines
in workflow YAML.

---

## C7 — Docs drift *(open)*

- `docs/user-guide/index.html` still says an EOS or EVopt server must already be
  running; `local_evopt` is the default and needs nothing.
- `configuration.html` documents `pv_forecast[].resource_id`; the schema moved it to
  `pv_forecast_source.resource_id`.

---

## Confirmed working — do not "fix"

- The `evcc` option is correctly **disabled** in both `pv_forecast_source.source` and
  `inverter.type` while `evcc.url` is the placeholder, with a tooltip.
- Conditional field visibility updates live on a source change.
- Pattern-backed validation blocks Next.
- Mobile layout at 390×844: no page overflow, wizard container within the viewport.
- The Skip button appears on the EVCC step only, and works.
