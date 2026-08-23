/**
 * Backup & Restore panel.
 *
 * Saves and restores the whole install — configuration plus the measured PV yield
 * history the autoscaler learns from. The configuration section keeps its own
 * export/import buttons; those stay config-only.
 *
 * A restore is always previewed before it is applied. It can remove settings and
 * overwrite a working configuration, which is not something to discover afterwards.
 */

// Narrowest a column may get before the grid drops to a single column. Matches the
// auto-fit minmax() widths used by the statistics and logging overlays.
const GRID_MIN = "330px";

const BACKUP_DATASETS = [
    {
        key: "settings",
        label: "Configuration",
        icon: "fa-gear",
        hint: "Every setting on this install",
    },
    {
        key: "pv_yield_history",
        label: "PV yield history",
        icon: "fa-solar-panel",
        hint: "Measured hourly yield the auto-scaler learns from",
    },
];

class BackupManager {
    /**
     * Initialize BackupManager.
     */
    constructor() {
        this.info = null;          // GET /api/backup/info
        // Kept apart on purpose: the two cards are separate decisions, and sharing one
        // set made ticking a box under Restore change what Download would write.
        this.forBackup = { settings: true, pv_yield_history: true };
        this.forRestore = { settings: true, pv_yield_history: true };
        this.mode = "replace";     // settings: replace | merge
        this.historyMode = "as_is"; // yield rows: as_is | seed
        this.pending = null;       // parsed file awaiting confirmation
        this.preview = null;       // dry-run result for this.pending
        this.result = null;        // outcome of the last applied restore
        this.busy = false;
    }

    // ── Public entry point ──────────────────────────────────────

    /**
     * Show the Backup & Restore overlay.
     */
    async showBackupMenu() {
        this.pending = null;
        this.preview = null;
        this.result = null;

        showFullScreenOverlay(this._header(), this._loading("Reading backup contents…"));
        try {
            const res = await fetch("api/backup/info");
            if (!res.ok) {
                throw new Error(`status ${res.status}`);
            }
            this.info = await res.json();
            this._render();
        } catch (err) {
            this._renderInto(this._error(
                "Could not read what this install holds.",
                err.message || err
            ));
        }
    }

    // ── Rendering ───────────────────────────────────────────────

    /**
     * Overlay header markup.
     * @returns {string} HTML
     */
    _header() {
        return `
            <div style="display:flex;align-items:center;gap:10px;">
                <i class="fa-solid fa-box-archive" style="color:#cccccc;"></i>
                <span>Backup &amp; Restore</span>
            </div>`;
    }

    /**
     * Replace the overlay body.
     * @param {string} html - Body markup
     */
    _renderInto(html) {
        const content = document.getElementById("full_screen_content");
        if (content) {
            content.innerHTML = html;
        }
    }

    /**
     * Render the whole panel from current state.
     */
    _render() {
        // Mid-restore the backup card is just noise, and leaving it on screen pushed the
        // Confirm button below the fold. Show one thing at a time.
        const busy = this.preview || this.result;

        // Two independent actions sit side by side where there is room and stack where
        // there is not, matching the auto-fit grids the other overlays use. A restore in
        // progress takes the full width instead — it has the most to say.
        const body = busy
            ? this._restoreCard()
            : `<div style="display:grid;
                          grid-template-columns:repeat(auto-fit, minmax(${GRID_MIN}, 1fr));
                          gap:15px;">
                   ${this._backupCard()}
                   ${this._restoreCard()}
               </div>`;

        this._renderInto(`${body}${busy ? "" : this._explainer()}`);

        const content = document.getElementById("full_screen_content");
        if (content) {
            content.scrollTop = 0;
        }
    }

    /**
     * Closing prose, in the same place and style as the other overlays' "How it works".
     *
     * Also stops the panel from being two short cards above a large empty area on a
     * desktop screen, which is what it looked like before.
     * @returns {string} HTML
     */
    _explainer() {
        const days = (this.info && this.info.retention_days) || 7;
        return `
            <div style="padding:15px 5px;color:#aaa;font-size:0.88em;line-height:1.6;">
                <div style="font-weight:bold;color:#ccc;margin-bottom:10px;">How it works:</div>
                <p style="margin:0 0 10px;">
                    A backup holds everything this install keeps: every configuration
                    setting, and the hourly record of measured PV yield against forecast
                    that <strong>PV Auto-Scaling</strong> derives its correction factors
                    from. That measured history is stored nowhere else and is deleted on a
                    rolling ${days}-day window, so a backup is the only way to keep it.
                </p>
                <p style="margin:0 0 10px;">
                    <strong>Restoring never applies straight away.</strong> The file is
                    inspected first and you are shown what would change — including any
                    settings that would be removed — before anything is written. Settings
                    that can be applied live take effect immediately; the rest raise the
                    usual restart banner.
                </p>
                <p style="margin:0;">
                    A backup older than the retention window can have its history
                    <strong>shifted into the current window</strong>, keeping each hour's
                    measured-to-forecast ratio so scaling works from the first run instead
                    of after days of re-learning. Those rows are marked as seeded, never
                    passed off as measured here.
                </p>
            </div>`;
    }

    /**
     * "Create a backup" card.
     * @returns {string} HTML
     */
    _backupCard() {
        const history = (this.info && this.info.pv_yield_history) || {};
        const settings = (this.info && this.info.settings) || {};
        const span = history.oldest && history.newest
            ? `${this._day(history.oldest)} → ${this._day(history.newest)}`
            : "nothing recorded yet";

        const details = {
            settings: `${settings.count || 0} settings`,
            pv_yield_history: history.available === false
                ? "unavailable"
                : `${history.count || 0} hours · ${this._escape(span)}`,
        };

        return this._card("fa-download", "Create a backup", `
            ${this._datasetPicker("backup", details)}
            ${this._warning(`
                The backup file contains your access tokens and inverter password in
                <strong>plain text</strong>. It has to — a masked file could not restore.
                Keep it somewhere you would keep a password.
            `)}
            <div style="margin-top:14px;">
                <button onclick="backupManager._download()" class="config-btn"
                        style="background:#4a9eff;color:#fff;border:none;border-radius:6px;
                               padding:9px 16px;cursor:pointer;font-size:0.9em;">
                    <i class="fas fa-download"></i> Download backup
                </button>
            </div>
        `, "everything this install keeps");
    }

    /**
     * "Restore from a backup" card — picker, preview or result depending on state.
     * @returns {string} HTML
     */
    _restoreCard() {
        if (this.result) {
            return this._card("fa-check", "Restore complete", this._resultBody());
        }
        if (this.preview) {
            return this._card("fa-list-check", "Review before restoring", this._previewBody());
        }
        return this._card("fa-upload", "Restore from a backup", `
            <p style="margin:0 0 12px;color:#bbb;font-size:0.9em;">
                Pick a backup file. Nothing is written until you confirm what it would do.
            </p>
            <input type="file" id="backup-restore-file" accept=".json" style="display:none;"
                   onchange="backupManager._pick(this.files[0])">
            <button onclick="document.getElementById('backup-restore-file').click()"
                    style="background:#5a5a5a;color:#e0e0e0;border:1px solid rgba(255,255,255,0.15);
                           border-radius:6px;padding:9px 16px;cursor:pointer;font-size:0.9em;
                           align-self:flex-start;">
                <i class="fas fa-folder-open"></i> Choose backup file…
            </button>
        `, "previewed before anything is written");
    }

    /**
     * The dry-run preview: what a restore would change.
     * @returns {string} HTML
     */
    _previewBody() {
        const p = this.preview;
        const settings = p.settings;
        const history = p.pv_yield_history;
        const rows = [];

        rows.push(this._metaRow("File", this._escape(this.pending.name)));
        rows.push(this._metaRow("Written", p.exported_at
            ? this._escape(this._day(p.exported_at))
            : "<span style='color:#ffc107;'>unknown — older config export</span>"));
        if (settings && settings.invalid.length) {
            rows.push(this._metaRow(
                "Values rejected",
                `<span style="color:#ffc107;">${settings.invalid.length} — skipped</span>`
            ));
        }
        if (history && history.available !== false) {
            if (history.skipped) {
                rows.push(this._metaRow(
                    "Yield rows unusable",
                    `<span style="color:#ffc107;">${history.skipped} — skipped</span>`
                ));
            }
            // Without these two the dataset line reads "2 of 6 hours" with no
            // explanation of where the other four went.
            if (history.collisions) {
                rows.push(this._metaRow(
                    "Hours already measured here",
                    `${history.collisions} — will be left untouched`
                ));
            }
            if (history.dropped_old) {
                rows.push(this._metaRow(
                    "Hours too old to keep",
                    `${history.dropped_old} — will not be written`
                ));
            }
        }

        // What each dataset would actually do, shown against its own checkbox rather
        // than as a second list repeating the same two labels.
        const details = {};
        if (settings) {
            details.settings = `${settings.imported} to write, ${settings.changed} changed`
                + (settings.removed.length
                    ? `, <span style="color:#ffc107;">${settings.removed.length} removed</span>`
                    : "");
        }
        if (history && history.available !== false) {
            details.pv_yield_history = `${history.valid || 0} of ${history.total || 0} hours`;
        }

        // Wide screens put "what is in the file" beside "how to restore it" instead of
        // running one long column the user has to scroll to reach the buttons.
        return `
            <div style="display:grid;
                        grid-template-columns:repeat(auto-fit, minmax(${GRID_MIN}, 1fr));
                        gap:15px;">
                <div style="background-color:rgba(255,255,255,0.05);border-radius:6px;
                            padding:12px;border-left:3px solid #4a9eff;">
                    <div style="font-weight:600;color:#ddd;margin-bottom:10px;">
                        What this file holds
                    </div>
                    <div style="display:flex;flex-direction:column;gap:6px;font-size:0.9em;
                                margin-bottom:14px;">
                        ${rows.join("")}
                    </div>
                    ${this._datasetPicker("restore", details)}
                </div>
                <div style="background-color:rgba(255,255,255,0.05);border-radius:6px;
                            padding:12px;border-left:3px solid #4a9eff;">
                    ${this._modePicker()}
                    ${this._historyPicker()}
                </div>
            </div>
            ${this._removalWarning()}
            ${this._invalidWarning()}
            ${this._anySelectedForRestore() ? "" : this._warning(
                "Nothing is selected, so there is nothing to restore."
            )}
            <div style="margin-top:16px;display:flex;gap:10px;flex-wrap:wrap;">
                <button onclick="backupManager._confirm()"
                        ${this.busy || !this._anySelectedForRestore() ? "disabled" : ""}
                        style="background:#4a9eff;color:#fff;border:none;border-radius:6px;
                               padding:9px 16px;cursor:pointer;font-size:0.9em;
                               opacity:${this.busy || !this._anySelectedForRestore()
                                   ? "0.6" : "1"};">
                    <i class="fas fa-check"></i> Restore now
                </button>
                <button onclick="backupManager._cancel()"
                        style="background:transparent;color:#bbb;border:1px solid rgba(255,255,255,0.2);
                               border-radius:6px;padding:9px 16px;cursor:pointer;font-size:0.9em;">
                    Cancel
                </button>
            </div>`;
    }

    /**
     * What the restore actually did.
     * @returns {string} HTML
     */
    _resultBody() {
        const r = this.result;
        const settings = r.settings;
        const history = r.pv_yield_history;
        const lines = [];

        if (settings) {
            lines.push(this._tile("Settings restored", settings.imported));
            if (settings.removed.length) {
                lines.push(this._tile("Settings removed", settings.removed.length));
            }
            if (settings.invalid.length) {
                lines.push(this._tile("Values skipped", settings.invalid.length, "#ffc107"));
            }
        }
        if (history && history.available !== false) {
            lines.push(this._tile("Yield hours restored", history.imported || 0));
            if (history.shift_days > 0) {
                lines.push(this._tile(
                    "History shifted", `${history.shift_days}d`, "#ffc107",
                    "forward — marked as seeded"
                ));
            }
            if (history.collisions) {
                lines.push(this._tile(
                    "Hours already measured here", history.collisions, null, "left untouched"
                ));
            }
            if (history.dropped_old) {
                lines.push(this._tile(
                    "Hours too old to keep", history.dropped_old, null, "not written"
                ));
            }
        }

        const restart = (settings && settings.restart_required) || [];
        const outside = history ? history.outside_retention : 0;

        return `
            <div style="display:grid;
                        grid-template-columns:repeat(auto-fit, minmax(200px, 1fr));
                        gap:10px;">
                ${lines.join("")}
            </div>
            ${restart.length ? this._warning(`
                <strong>Restart required.</strong> ${restart.length} restored setting(s)
                only take effect after EOS connect restarts. Everything else is already live.
            `) : ""}
            ${outside ? this._warning(`
                ${outside} restored hour(s) fall outside the ${history.retention_days}-day
                retention window and will be deleted at the next collection. Raise
                <em>PV auto-scaling → retention days</em> and restore again to keep them,
                or restore with time-shifting.
            `) : ""}
            <div style="margin-top:16px;display:flex;gap:10px;">
                <button onclick="backupManager.showBackupMenu()"
                        style="background:#5a5a5a;color:#e0e0e0;border:1px solid rgba(255,255,255,0.15);
                               border-radius:6px;padding:9px 16px;cursor:pointer;font-size:0.9em;">
                    Done
                </button>
            </div>`;
    }

    // ── Pickers ─────────────────────────────────────────────────

    /**
     * Dataset checkboxes, each with what it currently amounts to.
     * @param {string} scope - "backup" or "restore"; selects which selection it drives
     * @param {object} details - Per-dataset right-hand summary, keyed by dataset key
     * @returns {string} HTML
     */
    _datasetPicker(scope, details = {}) {
        const selection = scope === "backup" ? this.forBackup : this.forRestore;
        const items = BACKUP_DATASETS.map(ds => {
            const unavailable = ds.key === "pv_yield_history"
                && this.info && this.info.pv_yield_history
                && this.info.pv_yield_history.available === false;
            const detail = details[ds.key];
            // Beside the label where there is room; underneath it on a phone, where a
            // nowrap detail column squeezes the label into a two-character ribbon.
            const narrow = typeof isMobile === "function" && isMobile();
            const detailHtml = detail
                ? narrow
                    ? `<span style="display:block;margin-left:26px;margin-top:3px;
                                    color:#bbb;">${detail}</span>`
                    : `<span style="color:#bbb;white-space:nowrap;">${detail}</span>`
                : "";
            return `
                <label for="ds-${scope}-${ds.key}"
                       style="display:flex;align-items:flex-start;gap:9px;cursor:${
                    unavailable ? "not-allowed" : "pointer"};opacity:${unavailable ? "0.5" : "1"};">
                    <input type="checkbox" id="ds-${scope}-${ds.key}"
                           ${selection[ds.key] && !unavailable ? "checked" : ""}
                           ${unavailable ? "disabled" : ""}
                           onchange="backupManager._toggle('${scope}', '${ds.key}', this.checked)"
                           style="margin-top:3px;">
                    <span style="flex:1;min-width:0;">
                        <i class="fa-solid ${ds.icon}" style="width:18px;color:#888;"></i>
                        ${ds.label}
                        <span style="display:block;margin-left:26px;color:#8d8d8d;font-size:0.82em;">
                            ${ds.hint}
                        </span>
                        ${narrow ? detailHtml : ""}
                    </span>
                    ${narrow ? "" : detailHtml}
                </label>`;
        }).join("");

        return `<div style="display:flex;flex-direction:column;gap:12px;
                            font-size:0.9em;">${items}</div>`;
    }

    /**
     * Merge / replace choice for settings.
     * @returns {string} HTML
     */
    _modePicker() {
        if (!this.forRestore.settings) {
            return "";
        }
        return this._choice("How to restore the configuration", [
            {
                id: "backup-mode-replace",
                group: "backup-mode",
                value: "replace",
                checked: this.mode === "replace",
                label: "Replace",
                hint: "The configuration ends up exactly as in the file. Settings the file "
                    + "does not contain are removed and fall back to their defaults.",
                on: "backupManager._setMode('replace')",
            },
            {
                id: "backup-mode-merge",
                group: "backup-mode",
                value: "merge",
                checked: this.mode === "merge",
                label: "Merge",
                hint: "Apply what the file contains and leave everything else as it is.",
                on: "backupManager._setMode('merge')",
            },
        ]);
    }

    /**
     * As-is / seed choice for the yield history.
     * @returns {string} HTML
     */
    _historyPicker() {
        const history = this.preview && this.preview.pv_yield_history;
        if (!this.forRestore.pv_yield_history || !history || history.available === false
            || !history.valid) {
            return "";
        }
        const stale = history.seed_recommended;
        const shift = history.shift_days || 0;
        const seedHint = stale
            ? `The newest measurement in this file is ${history.age_days} days old — past the
               ${history.retention_days}-day window, so it would be deleted at the next
               collection. Shifting it ${shift || "a few"} day(s) forward keeps each hour's
               measured-to-forecast ratio and gives you working scale factors immediately.
               The rows are marked as seeded, not measured here.`
            : `Not recommended for this file: it is only ${history.age_days} day(s) old, so
               the shifted hours could land beside days this system already measured and
               count the same yield twice.`;

        return this._choice("How to restore the measured history", [
            {
                id: "backup-history-as-is",
                group: "backup-history-mode",
                value: "as_is",
                checked: this.historyMode === "as_is",
                label: "Keep original dates",
                hint: "Restore each hour at the time it was recorded.",
                on: "backupManager._setHistoryMode('as_is')",
            },
            {
                id: "backup-history-seed",
                group: "backup-history-mode",
                value: "seed",
                checked: this.historyMode === "seed",
                label: `Shift into the current window${stale ? " — recommended" : ""}`,
                hint: seedHint,
                on: "backupManager._setHistoryMode('seed')",
            },
        ]);
    }

    /**
     * Spell out which settings replace mode would remove.
     * @returns {string} HTML
     */
    _removalWarning() {
        const settings = this.preview && this.preview.settings;
        if (!settings || !settings.removed.length) {
            return "";
        }
        const shown = settings.removed.slice(0, 25).map(k => this._escape(k)).join(", ");
        const rest = settings.removed.length - 25;
        return this._warning(`
            <strong>${settings.removed.length} setting(s) will be removed</strong> and revert
            to their defaults, because this file does not contain them. This is normal when
            the backup came from an older version.
            <div style="margin-top:8px;font-family:monospace;font-size:0.85em;color:#ddd;
                        word-break:break-all;">${shown}${rest > 0 ? ` … +${rest} more` : ""}</div>
        `);
    }

    /**
     * List values the backend refused.
     * @returns {string} HTML
     */
    _invalidWarning() {
        const settings = this.preview && this.preview.settings;
        if (!settings || !settings.invalid.length) {
            return "";
        }
        const shown = settings.invalid.slice(0, 10)
            .map(e => `${this._escape(e.key)}: ${this._escape(e.error)}`)
            .join("<br>");
        return this._warning(`
            <strong>${settings.invalid.length} value(s) will be skipped</strong> —
            they are no longer valid for this version. Everything else still restores.
            <div style="margin-top:8px;font-size:0.85em;color:#ddd;">${shown}</div>
        `);
    }

    // ── Actions ─────────────────────────────────────────────────

    /**
     * Toggle a dataset on or off.
     * @param {string} scope - "backup" or "restore"
     * @param {string} key - Dataset key
     * @param {boolean} on - New state
     */
    _toggle(scope, key, on) {
        if (scope === "backup") {
            this.forBackup[key] = on;
            this._render();
            return;
        }
        this.forRestore[key] = on;
        // The preview is scoped to the selected datasets, so it has to be recomputed
        // rather than left showing counts for a selection that no longer applies.
        this._refreshPreview();
    }

    /**
     * Set the settings restore mode.
     * @param {string} mode - "replace" or "merge"
     */
    _setMode(mode) {
        this.mode = mode;
        this._refreshPreview();
    }

    /**
     * Set the yield history restore mode.
     * @param {string} mode - "as_is" or "seed"
     */
    _setHistoryMode(mode) {
        this.historyMode = mode;
        this._refreshPreview();
    }

    /**
     * Whether at least one dataset is ticked for restoring.
     * @returns {boolean} True when there is something to do
     */
    _anySelectedForRestore() {
        return Object.values(this.forRestore).some(Boolean);
    }

    /**
     * Build the query string both preview and apply use.
     * @param {boolean} dryRun - Whether to ask for a preview only
     * @returns {string} Query string
     */
    _query(dryRun) {
        const include = Object.keys(this.forRestore)
            .filter(k => this.forRestore[k])
            .join(",");
        const params = [
            `include=${encodeURIComponent(include)}`,
            `mode=${this.mode}`,
            `history_mode=${this.historyMode}`,
        ];
        if (dryRun) {
            params.push("dry_run=1");
        }
        return `?${params.join("&")}`;
    }

    /**
     * Download the selected datasets as a file.
     */
    async _download() {
        const include = Object.keys(this.forBackup).filter(k => this.forBackup[k]);
        if (!include.length) {
            this._toast("Select at least one thing to back up.", "warning");
            return;
        }
        try {
            const res = await fetch(`api/backup/export?include=${include.join(",")}`);
            if (!res.ok) {
                throw new Error(`status ${res.status}`);
            }
            const data = await res.json();
            const blob = new Blob([JSON.stringify(data, null, 2)], {
                type: "application/json",
            });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `eos_connect_backup_${new Date().toISOString().slice(0, 10)}.json`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            this._toast("Backup downloaded. Keep it somewhere safe — it holds your tokens.",
                "success", 6000);
        } catch (err) {
            this._toast("Backup failed: " + (err.message || err), "error");
        }
    }

    /**
     * Parse a chosen file and ask the backend what restoring it would do.
     * @param {File} file - The selected file
     */
    async _pick(file) {
        if (!file) {
            return;
        }
        let data;
        try {
            data = JSON.parse(await file.text());
        } catch (err) {
            this._toast("That file is not valid JSON.", "error");
            return;
        }
        if (typeof data !== "object" || data === null || Array.isArray(data)) {
            this._toast("That file does not look like a backup.", "error");
            return;
        }

        this.pending = { name: file.name, data };
        // A file with no history section has nothing to restore into that dataset.
        if (!Array.isArray(data.pv_yield_history) || !data.pv_yield_history.length) {
            this.forRestore.pv_yield_history = false;
        }
        await this._refreshPreview(true);
    }

    /**
     * Re-run the dry run for the current file and options.
     * @param {boolean} firstTime - Whether this is the initial preview for a new file
     */
    async _refreshPreview(firstTime = false) {
        if (!this.pending) {
            return;
        }
        if (!this._anySelectedForRestore()) {
            // Nothing to preview, but keep the card on screen so the user can tick
            // something back on rather than losing the chosen file.
            this._render();
            return;
        }
        try {
            const res = await fetch(`api/backup/import${this._query(true)}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(this.pending.data),
            });
            const preview = await res.json();
            if (!res.ok) {
                throw new Error(preview.error || `status ${res.status}`);
            }
            this.preview = preview;
            // Offer the shift by default when the file is too old to be useful as-is,
            // which is also the only case where it cannot duplicate measured days.
            const history = preview.pv_yield_history;
            if (firstTime && history && history.seed_recommended) {
                this.historyMode = "seed";
                await this._refreshPreview();
                return;
            }
            this._render();
        } catch (err) {
            this._toast("Could not read that backup: " + (err.message || err), "error");
        }
    }

    /**
     * Apply the previewed restore.
     */
    async _confirm() {
        if (!this.pending || this.busy || !this._anySelectedForRestore()) {
            return;
        }
        this.busy = true;
        this._render();
        try {
            const res = await fetch(`api/backup/import${this._query(false)}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(this.pending.data),
            });
            const result = await res.json();
            if (!res.ok) {
                throw new Error(result.error || `status ${res.status}`);
            }
            this.result = result;
            this.preview = null;
            this.pending = null;
            this._render();
            this._afterRestore(result);
        } catch (err) {
            this._toast("Restore failed: " + (err.message || err), "error");
        } finally {
            this.busy = false;
            if (!this.result) {
                this._render();   // re-enable the button after a failure
            }
        }
    }

    /**
     * Discard a pending restore.
     */
    _cancel() {
        this.pending = null;
        this.preview = null;
        this._render();
    }

    /**
     * Refresh the rest of the UI so it reflects the restored config.
     * @param {object} result - The import response
     */
    _afterRestore(result) {
        const restart = (result.settings && result.settings.restart_required) || [];
        if (restart.length && typeof MenuNotifications !== "undefined") {
            MenuNotifications.setRestartPending(true);
        }
        if (typeof configurationManager !== "undefined" && configurationManager) {
            // Drop the cached schema values so reopening Configuration shows the
            // restored config rather than what was on screen before.
            configurationManager.values = {};
            configurationManager.originalValues = {};
            configurationManager.restartFields = restart;
        }
    }

    // ── Small helpers ───────────────────────────────────────────

    /**
     * Section wrapper, styled like the sections in the other full-screen overlays.
     * @param {string} icon - Font Awesome icon class
     * @param {string} title - Section title
     * @param {string} body - Section body markup
     * @param {string} note - Optional right-aligned subtitle
     * @returns {string} HTML
     */
    _card(icon, title, body, note = "") {
        return `
            <div style="background-color:rgba(0,0,0,0.2);border-radius:8px;padding:15px;
                        display:flex;flex-direction:column;">
                <div style="display:flex;justify-content:space-between;align-items:center;
                            gap:12px;margin-bottom:12px;">
                    <div style="font-weight:bold;color:#ccc;">
                        <i class="fa-solid ${icon}" style="margin-right:6px;"></i>${title}
                    </div>
                    ${note && !(typeof isMobile === "function" && isMobile())
                        ? `<div style="font-size:0.85em;color:#888;text-align:right;">${note}</div>`
                        : ""}
                </div>
                ${body}
            </div>`;
    }

    /**
     * A labelled radio group.
     *
     * Each option carries an explicit `id` and shared `group` name so the label is
     * properly associated with its input rather than relying on nesting alone.
     * @param {string} title - Group title
     * @param {Array<object>} options - Radio option descriptors: id, group, checked,
     *     label, hint, on
     * @returns {string} HTML
     */
    _choice(title, options) {
        const radios = options.map(o => `
            <label for="${o.id}" style="display:flex;align-items:flex-start;gap:9px;cursor:pointer;">
                <input type="radio" id="${o.id}" name="${o.group}"
                       ${o.checked ? "checked" : ""} onchange="${o.on}"
                       style="margin-top:3px;">
                <span>
                    ${o.label}
                    <span style="display:block;color:#8d8d8d;font-size:0.85em;margin-top:2px;">
                        ${o.hint}
                    </span>
                </span>
            </label>`).join("");

        return `
            <div style="margin-bottom:16px;">
                <div style="font-weight:600;color:#ddd;margin-bottom:10px;">${title}</div>
                <div style="display:flex;flex-direction:column;gap:10px;font-size:0.9em;">
                    ${radios}
                </div>
            </div>`;
    }

    /**
     * Amber warning box.
     * @param {string} html - Warning body
     * @returns {string} HTML
     */
    _warning(html) {
        return `
            <div style="margin-top:14px;background:rgba(255,193,7,0.1);
                        border-left:3px solid #ffc107;border-radius:4px;padding:11px 13px;
                        font-size:0.87em;color:#ddd;line-height:1.5;">
                <i class="fas fa-exclamation-triangle"
                   style="color:#ffc107;margin-right:7px;"></i>${html}
            </div>`;
    }

    /**
     * A single count, rendered like the stat tiles in the other overlays.
     * @param {string} label - What is being counted
     * @param {string|number} value - The count
     * @param {string} color - Optional accent for the value
     * @param {string} note - Optional qualifier under the value
     * @returns {string} HTML
     */
    _tile(label, value, color = null, note = "") {
        return `
            <div style="background-color:rgba(255,255,255,0.05);border-radius:6px;
                        padding:12px;text-align:center;border-left:3px solid ${
                            color || "#4a9eff"};">
                <div style="font-size:0.85em;color:#888;margin-bottom:6px;">${label}</div>
                <div style="font-size:1.6em;font-weight:bold;font-family:monospace;
                            color:${color || "#4caf50"};">${value}</div>
                ${note
                    ? `<div style="font-size:0.78em;color:#aaa;margin-top:4px;">${note}</div>`
                    : ""}
            </div>`;
    }

    /**
     * A label/value row.
     * @param {string} label - Row label
     * @param {string} value - Row value markup
     * @returns {string} HTML
     */
    _metaRow(label, value) {
        return `
            <div style="display:flex;justify-content:space-between;gap:14px;">
                <span style="color:#9d9d9d;">${label}</span>
                <span style="text-align:right;">${value}</span>
            </div>`;
    }

    /**
     * Centred spinner.
     * @param {string} text - Loading message
     * @returns {string} HTML
     */
    _loading(text) {
        return `
            <div style="display:flex;justify-content:center;align-items:center;height:100%;
                        color:#888;">
                <i class="fas fa-spinner fa-spin" style="font-size:1.6em;margin-right:12px;"></i>
                ${text}
            </div>`;
    }

    /**
     * Centred error block.
     * @param {string} title - Headline
     * @param {string} detail - Detail line
     * @returns {string} HTML
     */
    _error(title, detail) {
        return `
            <div style="text-align:center;color:#dc3545;padding:40px 20px;">
                <i class="fas fa-exclamation-triangle" style="font-size:1.8em;"></i>
                <div style="margin-top:12px;color:#e0e0e0;">${this._escape(title)}</div>
                <div style="margin-top:6px;color:#999;font-size:0.85em;">
                    ${this._escape(String(detail))}
                </div>
            </div>`;
    }

    /**
     * Date portion of an ISO timestamp.
     * @param {string} iso - ISO timestamp
     * @returns {string} YYYY-MM-DD
     */
    _day(iso) {
        return String(iso || "").slice(0, 10);
    }

    /**
     * Escape text for interpolation into markup.
     * @param {string} text - Raw text
     * @returns {string} Escaped text
     */
    _escape(text) {
        const div = document.createElement("div");
        div.textContent = text === null || text === undefined ? "" : String(text);
        return div.innerHTML;
    }

    /**
     * Show a toast, reusing the configuration panel's implementation.
     * @param {string} message - Toast text
     * @param {string} type - info | success | warning | error
     * @param {number} duration - Milliseconds on screen
     */
    _toast(message, type = "info", duration = 3500) {
        if (typeof configurationManager === "undefined") {
            return;
        }
        if (!configurationManager) {
            configurationManager = new ConfigurationManager();
        }
        configurationManager._showToast(message, type, duration);
    }
}

let backupManager;

/**
 * Open the Backup & Restore panel from the main menu.
 */
function showBackupMenu() {
    if (!backupManager) {
        backupManager = new BackupManager();
    }
    backupManager.showBackupMenu();
}
