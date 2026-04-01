/**
 * ConfigurationManager — Web Config UI for EOS Connect
 *
 * Fetches the config schema + current values from /api/config,
 * renders a section-based editor with level filtering, and
 * saves changes back via PUT /api/config.
 */

/* global showFullScreenOverlay, closeFullScreenOverlay, isMobile */

// ── Section metadata (icon + display name) ──────────────────────
const CONFIG_SECTIONS = {
    data_source:       { icon: "fa-plug",              label: "Data Source" },
    load:              { icon: "fa-bolt",               label: "Load" },
    eos:               { icon: "fa-server",             label: "Optimizer" },
    price:             { icon: "fa-coins",              label: "Price" },
    battery:           { icon: "fa-battery-full",       label: "Battery" },
    pv_forecast_source:{ icon: "fa-sun",                label: "PV Source" },
    pv_forecast:       { icon: "fa-solar-panel",        label: "PV Forecast" },
    inverter:          { icon: "fa-microchip",          label: "Inverter" },
    evcc:              { icon: "fa-car",                label: "EVCC" },
    mqtt:              { icon: "fa-tower-broadcast",    label: "MQTT" },
    system:            { icon: "fa-gears",              label: "System" },
};

const LEVEL_ORDER = { getting_started: 0, standard: 1, expert: 2 };


class ConfigurationManager {
    /**
     * Initialize ConfigurationManager.
     */
    constructor() {
        this.schema = null;        // array of field defs from /api/config/schema
        this.values = {};          // flat {dot.key: value}
        this.originalValues = {};  // snapshot for dirty detection
        this.level = localStorage.getItem("config_level") || "standard";
        this.activeSection = null;
        this.toastContainer = null;
        this.restartFields = [];   // fields changed that need restart
    }

    // ── Public entry point ──────────────────────────────────────

    /**
     * Show the configuration overlay.
     */
    async showConfigurationMenu() {
        // Show loading state immediately
        const header = this._buildHeader();
        showFullScreenOverlay(header, `
            <div style="display:flex;justify-content:center;align-items:center;height:100%;color:#888;">
                <i class="fas fa-spinner fa-spin" style="font-size:2em;margin-right:12px;"></i>
                Loading configuration…
            </div>
        `);

        try {
            await this._loadData();
            this._renderFull();
        } catch (err) {
            console.error("[ConfigurationManager] Failed to load config:", err);
            document.getElementById("full_screen_content").innerHTML = `
                <div style="text-align:center;padding:40px;color:#dc3545;">
                    <i class="fas fa-exclamation-triangle" style="font-size:2em;margin-bottom:12px;"></i>
                    <p>Failed to load configuration</p>
                    <p style="font-size:0.85em;color:#888;">${err.message || err}</p>
                </div>
            `;
        }
    }

    // ── Data loading ────────────────────────────────────────────

    /**
     * Fetch schema and current values from the API.
     */
    async _loadData() {
        const [schemaRes, valuesRes] = await Promise.all([
            fetch("/api/config/schema"),
            fetch("/api/config/export"),
        ]);

        if (!schemaRes.ok) {
            throw new Error(`Schema: ${schemaRes.status}`);
        }
        if (!valuesRes.ok) {
            throw new Error(`Values: ${valuesRes.status}`);
        }

        this.schema = await schemaRes.json();
        const raw = await valuesRes.json();

        // Flatten export to dot-notation
        this.values = {};
        for (const [k, v] of Object.entries(raw)) {
            this.values[k] = v;
        }

        // Fill missing keys with schema defaults
        for (const f of this.schema) {
            if (!(f.key in this.values) && f.key.indexOf("pv_forecast.") !== 0) {
                this.values[f.key] = f.default;
            }
        }

        this.originalValues = JSON.parse(JSON.stringify(this.values));
    }

    // ── Full render ─────────────────────────────────────────────

    /**
     * Render the full config layout (nav + content).
     */
    _renderFull() {
        const header = this._buildHeader();
        const content = `<div class="config-layout" id="cfg-layout">
            <div class="config-nav" id="cfg-nav">
                ${this._renderNav()}
            </div>
            <div class="config-content" id="cfg-content">
                <div style="display:flex;justify-content:center;align-items:center;height:100%;color:#888;">
                    <i class="fas fa-arrow-left" style="margin-right:10px;"></i>
                    Select a section from the menu
                </div>
            </div>
        </div>`;

        // Update overlay in place
        const headerEl = document.getElementById("full_screen_header");
        const contentEl = document.getElementById("full_screen_content");
        if (headerEl) {
            headerEl.innerHTML = header + this._buildCloseBtn();
        }
        if (contentEl) {
            contentEl.innerHTML = content;
        }

        // Auto-select first section
        const sections = this._orderedSections();
        if (sections.length > 0) {
            this._selectSection(sections[0]);
        }
    }

    // ── Header ──────────────────────────────────────────────────

    /**
     * Build the overlay header HTML.
     * @returns {string} Header HTML
     */
    _buildHeader() {
        const levelOptions = [
            { val: "getting_started", label: "Getting Started" },
            { val: "standard",        label: "Standard" },
            { val: "expert",          label: "Expert" },
        ];
        const opts = levelOptions.map(o =>
            `<option value="${o.val}" ${o.val === this.level ? "selected" : ""}>${o.label}</option>`
        ).join("");

        return `
            <div style="display:flex;align-items:center;gap:10px;flex:1;">
                <button class="config-mobile-back config-btn config-btn-secondary"
                        onclick="configurationManager._mobileBack()"
                        style="padding:6px 10px;font-size:0.85em;">
                    <i class="fas fa-arrow-left"></i>
                </button>
                <i class="fas fa-gear" style="color:#cccccc;"></i>
                <span>Configuration</span>
                <select class="config-level-select"
                        onchange="configurationManager._setLevel(this.value)"
                        title="Disclosure level">${opts}</select>
                <button onclick="closeFullScreenOverlay(); showSetupWizard();"
                        style="margin-left:auto;background:#4a9eff;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-size:0.8em;cursor:pointer;display:inline-flex;align-items:center;gap:5px;"
                        title="Run Setup Wizard">
                    <i class="fas fa-wand-magic-sparkles"></i>
                    <span class="config-desktop-only">Wizard</span>
                </button>
            </div>`;
    }

    /**
     * Build the close button for the overlay header.
     * @returns {string} Close button HTML
     */
    _buildCloseBtn() {
        const size = isMobile() ? "28px" : "30px";
        return `<button onclick="closeFullScreenOverlay()" style="
            background:none;border:none;color:lightgray;font-size:1.5em;cursor:pointer;
            padding:0;width:${size};height:${size};display:flex;align-items:center;
            justify-content:center;border-radius:50%;transition:background-color 0.2s;"
            onmouseover="this.style.backgroundColor='rgba(255,255,255,0.1)'"
            onmouseout="this.style.backgroundColor='transparent'">×</button>`;
    }

    // ── Navigation ──────────────────────────────────────────────

    /**
     * Get the ordered list of section keys.
     * @returns {string[]} Section keys
     */
    _orderedSections() {
        const seen = [];
        for (const f of this.schema) {
            if (!seen.includes(f.section)) {
                seen.push(f.section);
            }
        }
        return seen;
    }

    /**
     * Render the section navigation.
     * @returns {string} Nav HTML
     */
    _renderNav() {
        return this._orderedSections().map(sec => {
            const meta = CONFIG_SECTIONS[sec] || { icon: "fa-circle", label: sec };
            const cls = sec === this.activeSection ? "active" : "";
            const visibleCount = this._fieldsForSection(sec).length;
            if (visibleCount === 0) {
                return "";
            }
            return `
                <div class="config-nav-item ${cls}"
                     data-section="${sec}"
                     onclick="configurationManager._selectSection('${sec}')">
                    <i class="fa-solid ${meta.icon}"></i>
                    <span>${meta.label}</span>
                </div>`;
        }).join("");
    }

    /**
     * Handle section selection.
     * @param {string} section - Section key
     */
    _selectSection(section) {
        this.activeSection = section;

        // Update nav active state
        document.querySelectorAll(".config-nav-item").forEach(el => {
            el.classList.toggle("active", el.dataset.section === section);
        });

        // On mobile: hide nav, show content
        if (isMobile()) {
            const nav = document.getElementById("cfg-nav");
            const content = document.getElementById("cfg-content");
            if (nav) {
                nav.classList.add("collapsed");
            }
            if (content) {
                content.classList.remove("hidden");
            }
        }

        // Render section content
        const contentEl = document.getElementById("cfg-content");
        if (contentEl) {
            if (section === "pv_forecast") {
                contentEl.innerHTML = this._renderPvForecastSection();
            } else {
                contentEl.innerHTML = this._renderSection(section);
            }
        }
    }

    /**
     * Mobile back button handler.
     */
    _mobileBack() {
        const nav = document.getElementById("cfg-nav");
        const content = document.getElementById("cfg-content");
        if (nav) {
            nav.classList.remove("collapsed");
        }
        if (content) {
            content.classList.add("hidden");
        }
    }

    // ── Level management ────────────────────────────────────────

    /**
     * Change the disclosure level.
     * @param {string} level - The new level
     */
    _setLevel(level) {
        this.level = level;
        localStorage.setItem("config_level", level);

        // Refresh nav to hide sections with 0 visible fields
        const navEl = document.getElementById("cfg-nav");
        if (navEl) {
            navEl.innerHTML = this._renderNav();
        }

        // Re-render current section
        if (this.activeSection) {
            this._selectSection(this.activeSection);
        }
    }

    // ── Section rendering ───────────────────────────────────────

    /**
     * Get fields for a section, filtered by current level.
     * @param {string} section - Section key
     * @returns {Object[]} Filtered field definitions
     */
    _fieldsForSection(section) {
        const maxLvl = LEVEL_ORDER[this.level] ?? 2;
        return this.schema.filter(f =>
            f.section === section && (LEVEL_ORDER[f.level] ?? 2) <= maxLvl
        );
    }

    /**
     * Render a full section (non-PV).
     * @param {string} section - Section key
     * @returns {string} Section HTML
     */
    _renderSection(section) {
        const meta = CONFIG_SECTIONS[section] || { icon: "fa-circle", label: section };
        const fields = this._fieldsForSection(section);

        // Group by display_group
        const groups = this._groupFields(fields);

        // Restart banner
        let html = `<div class="config-restart-banner" id="cfg-restart-banner">
            <i class="fas fa-rotate"></i>
            <span id="cfg-restart-msg">Restart required for changes to take effect.</span>
        </div>`;

        html += `<div class="config-section-title">
            <i class="fa-solid ${meta.icon}" style="color:#4a9eff;"></i>
            ${meta.label}
        </div>`;

        for (const [groupName, groupFields] of groups) {
            if (groupName) {
                html += `<div class="config-group">
                    <div class="config-group-title">${groupName}</div>
                    ${groupFields.map(f => this._renderField(f)).join("")}
                </div>`;
            } else {
                html += groupFields.map(f => this._renderField(f)).join("");
            }
        }

        html += this._renderActions(section);
        return html;
    }

    /**
     * Group fields by display_group, preserving order.
     * @param {Object[]} fields - Array of field definitions
     * @returns {[string, Object[]][]} Array of [groupName, fields] tuples
     */
    _groupFields(fields) {
        const groups = new Map();
        for (const f of fields) {
            const g = f.display_group || "";
            if (!groups.has(g)) {
                groups.set(g, []);
            }
            groups.get(g).push(f);
        }
        return Array.from(groups.entries());
    }

    // ── Individual field rendering ──────────────────────────────

    /**
     * Render a single config field.
     * @param {Object} f - Field definition from schema
     * @returns {string} Field HTML
     */
    _renderField(f) {
        const val = this.values[f.key] ?? f.default;
        const isHidden = this._isDependencyHidden(f) ? " hidden" : "";

        let inputHtml;
        switch (f.type) {
            case "bool":
                inputHtml = this._renderToggle(f, val);
                break;
            case "select":
                inputHtml = this._renderSelect(f, val);
                break;
            case "password":
                inputHtml = this._renderPassword(f, val);
                break;
            default:
                inputHtml = this._renderTextInput(f, val);
        }

        // Badges
        const badges = (f.labels || []).map(l => this._renderBadge(l)).join(" ");

        // Help button
        const helpBtn = f.description
            ? `<button class="config-help-btn" onclick="configurationManager._toggleHelp('${f.key}')" title="Help">
                 <i class="fas fa-circle-question"></i>
               </button>`
            : "";

        // Help text
        const docsLink = f.help_url
            ? ` <a href="https://ohand.github.io/EOS_connect/user-guide/${f.help_url}" target="_blank" rel="noopener">
                 Learn more <i class="fas fa-external-link-alt" style="font-size:0.8em;"></i></a>`
            : "";
        const helpText = `<div class="config-help-text" id="cfg-help-${this._cssKey(f.key)}">
            ${this._escapeHtml(f.description)}${docsLink}
        </div>`;

        // Pretty label
        const labelText = this._prettyLabel(f.key);

        return `
            <div class="config-field${isHidden}" data-key="${f.key}" id="cfg-field-${this._cssKey(f.key)}">
                <div class="config-field-label">
                    <span>${labelText}</span>
                    ${badges}
                    ${helpBtn}
                </div>
                <div class="config-field-input">
                    ${inputHtml}
                </div>
                ${helpText}
                <div class="config-field-error" id="cfg-err-${this._cssKey(f.key)}"></div>
            </div>`;
    }

    /**
     * Render a text or number input.
     * @param {Object} f - Field definition
     * @param {*} val - Current value
     * @returns {string} Input HTML
     */
    _renderTextInput(f, val) {
        const inputType = (f.type === "int" || f.type === "float") ? "number" : "text";
        let attrs = "";
        if (f.validation) {
            if (f.validation.min !== undefined) {
                attrs += ` min="${f.validation.min}"`;
            }
            if (f.validation.max !== undefined) {
                attrs += ` max="${f.validation.max}"`;
            }
        }
        if (f.type === "float") {
            attrs += ` step="any"`;
        }
        const displayVal = val !== null && val !== undefined ? val : "";
        return `<input class="config-input" type="${inputType}"
                       data-key="${f.key}"
                       value="${this._escapeAttr(String(displayVal))}"
                       ${attrs}
                       onchange="configurationManager._onFieldChange('${f.key}', this.value)">`;
    }

    /**
     * Render a select dropdown.
     * @param {Object} f - Field definition
     * @param {*} val - Current value
     * @returns {string} Select HTML
     */
    _renderSelect(f, val) {
        const choices = (f.validation && f.validation.choices) || [];
        const opts = choices.map(c => {
            const selected = String(c) === String(val) ? "selected" : "";
            return `<option value="${this._escapeAttr(String(c))}" ${selected}>${c}</option>`;
        }).join("");
        return `<select class="config-select" data-key="${f.key}"
                        onchange="configurationManager._onFieldChange('${f.key}', this.value)">
                    ${opts}
                </select>`;
    }

    /**
     * Render a boolean toggle switch.
     * @param {Object} f - Field definition
     * @param {*} val - Current value
     * @returns {string} Toggle HTML
     */
    _renderToggle(f, val) {
        const checked = val === true || val === "true" || val === "True" ? "checked" : "";
        return `<label class="config-toggle">
            <input type="checkbox" data-key="${f.key}" ${checked}
                   onchange="configurationManager._onFieldChange('${f.key}', this.checked)">
            <span class="config-toggle-slider"></span>
        </label>`;
    }

    /**
     * Render a password field with show/hide toggle.
     * @param {Object} f - Field definition
     * @param {*} val - Current value
     * @returns {string} Password HTML
     */
    _renderPassword(f, val) {
        const displayVal = val !== null && val !== undefined ? val : "";
        return `<div class="config-password-wrap">
            <input class="config-input" type="password"
                   id="cfg-pw-${this._cssKey(f.key)}"
                   data-key="${f.key}"
                   value="${this._escapeAttr(String(displayVal))}"
                   onchange="configurationManager._onFieldChange('${f.key}', this.value)">
            <button class="config-password-toggle"
                    onclick="configurationManager._togglePassword('${f.key}')"
                    title="Show / hide">
                <i class="fas fa-eye"></i>
            </button>
        </div>`;
    }

    // ── PV Forecast (array) section ─────────────────────────────

    /**
     * Render the PV Forecast section as an array of installation cards.
     * @returns {string} PV section HTML
     */
    _renderPvForecastSection() {
        const meta = CONFIG_SECTIONS.pv_forecast;
        const pvFields = this.schema.filter(f => f.section === "pv_forecast");
        const maxLvl = LEVEL_ORDER[this.level] ?? 2;
        const visibleFields = pvFields.filter(f => (LEVEL_ORDER[f.level] ?? 2) <= maxLvl);

        // Collect PV installations from store — keys like pv_forecast.0.name, pv_forecast.1.name, etc.
        const installations = this._getPvInstallations();

        let html = `<div class="config-restart-banner" id="cfg-restart-banner">
            <i class="fas fa-rotate"></i>
            <span id="cfg-restart-msg">Restart required for changes to take effect.</span>
        </div>`;

        html += `<div class="config-section-title">
            <i class="fa-solid ${meta.icon}" style="color:#4a9eff;"></i>
            ${meta.label}
        </div>
        <div class="config-section-desc">
            Each PV installation is configured separately. Add one card per roof / orientation.
        </div>`;

        if (installations.length === 0) {
            // Show at least one empty card
            html += this._renderPvCard(0, visibleFields, {});
        } else {
            installations.forEach((inst, idx) => {
                html += this._renderPvCard(idx, visibleFields, inst);
            });
        }

        html += `<button class="config-pv-add" onclick="configurationManager._addPvInstallation()">
            <i class="fas fa-plus"></i> Add PV Installation
        </button>`;

        html += this._renderActions("pv_forecast");
        return html;
    }

    /**
     * Render a single PV installation card.
     * @param {number} idx - Installation index
     * @param {Object[]} fieldDefs - Visible field definitions
     * @param {Object} values - Key-value map for this installation
     * @returns {string} Card HTML
     */
    _renderPvCard(idx, fieldDefs, values) {
        const name = values.name || `Installation ${idx + 1}`;
        let html = `<div class="config-pv-card" data-pv-idx="${idx}">
            <div class="config-pv-card-header">
                <span><i class="fas fa-solar-panel" style="margin-right:8px;color:#4a9eff;"></i>${this._escapeHtml(name)}</span>
                ${idx > 0 ? `<button class="config-btn config-btn-danger" style="padding:4px 10px;font-size:0.8em;"
                    onclick="configurationManager._removePvInstallation(${idx})">
                    <i class="fas fa-trash"></i>
                </button>` : ""}
            </div>`;

        for (const f of fieldDefs) {
            const subKey = f.key.split(".").pop(); // e.g. "name", "lat", "lon"
            const fullKey = `pv_forecast.${idx}.${subKey}`;
            const val = values[subKey] ?? f.default;

            // Build a pseudo-field for rendering
            const pf = { ...f, key: fullKey };
            html += this._renderField(pf);
        }

        html += "</div>";
        return html;
    }

    /**
     * Get PV installations from current values.
     * @returns {Object[]} Array of installation value maps
     */
    _getPvInstallations() {
        const instMap = {};
        for (const [k, v] of Object.entries(this.values)) {
            const m = k.match(/^pv_forecast\.(\d+)\.(.+)$/);
            if (m) {
                const idx = parseInt(m[1], 10);
                if (!instMap[idx]) {
                    instMap[idx] = {};
                }
                instMap[idx][m[2]] = v;
            }
        }
        return Object.keys(instMap).sort((a, b) => a - b).map(k => instMap[k]);
    }

    /**
     * Add a new empty PV installation.
     */
    _addPvInstallation() {
        const installations = this._getPvInstallations();
        const newIdx = installations.length;
        const pvFields = this.schema.filter(f => f.section === "pv_forecast");

        for (const f of pvFields) {
            const subKey = f.key.split(".").pop();
            this.values[`pv_forecast.${newIdx}.${subKey}`] = f.default;
        }

        // Re-render
        this._selectSection("pv_forecast");
    }

    /**
     * Remove a PV installation by index.
     * @param {number} idx - Installation index to remove
     */
    _removePvInstallation(idx) {
        // Remove keys for this index
        const prefix = `pv_forecast.${idx}.`;
        for (const k of Object.keys(this.values)) {
            if (k.startsWith(prefix)) {
                delete this.values[k];
            }
        }

        // Re-index remaining installations
        const installations = this._getPvInstallations();
        // Clear all pv_forecast keys
        for (const k of Object.keys(this.values)) {
            if (/^pv_forecast\.\d+\./.test(k)) {
                delete this.values[k];
            }
        }
        // Re-add with sequential indexes
        installations.forEach((inst, newIdx) => {
            for (const [subKey, val] of Object.entries(inst)) {
                this.values[`pv_forecast.${newIdx}.${subKey}`] = val;
            }
        });

        this._selectSection("pv_forecast");
    }

    // ── Actions bar (save / reset) ──────────────────────────────

    /**
     * Render the save/reset action bar for a section.
     * @param {string} section - Section key
     * @returns {string} Actions HTML
     */
    _renderActions(section) {
        return `<div class="config-actions">
            <button class="config-btn config-btn-primary"
                    onclick="configurationManager._saveSection('${section}')">
                <i class="fas fa-save"></i> Save
            </button>
            <button class="config-btn config-btn-secondary"
                    onclick="configurationManager._resetSection('${section}')">
                <i class="fas fa-undo"></i> Reset
            </button>
        </div>`;
    }

    // ── Field change handling ───────────────────────────────────

    /**
     * Handle a field value change from the UI.
     * @param {string} key - Dot-notation key
     * @param {*} value - New value
     */
    _onFieldChange(key, value) {
        this.values[key] = value;

        // Re-evaluate dependent field visibility
        this._updateDependencies(key);

        // Mark the input as changed
        const input = document.querySelector(`[data-key="${key}"]`);
        if (input && input.classList) {
            const original = this.originalValues[key];
            if (String(value) !== String(original)) {
                input.classList.add("changed");
            } else {
                input.classList.remove("changed");
            }
        }
    }

    // ── Dependency visibility ───────────────────────────────────

    /**
     * Check if a field should be hidden based on its depends_on rule.
     * @param {Object} f - Field definition
     * @returns {boolean} True if hidden
     */
    _isDependencyHidden(f) {
        if (!f.depends_on) {
            return false;
        }
        for (const [depKey, allowed] of Object.entries(f.depends_on)) {
            const currentVal = this.values[depKey] ?? this._getSchemaDefault(depKey);

            if (allowed === "!empty") {
                if (!currentVal || currentVal === "") {
                    return true;
                }
                continue;
            }

            if (Array.isArray(allowed)) {
                // Match against allowed values (compare as strings and native)
                const match = allowed.some(a =>
                    a === currentVal || String(a) === String(currentVal)
                );
                if (!match) {
                    return true;
                }
            }
        }
        return false;
    }

    /**
     * After a field changes, update visibility of dependent fields.
     * @param {string} changedKey - The key that changed
     */
    _updateDependencies(changedKey) {
        if (!this.schema) {
            return;
        }
        for (const f of this.schema) {
            if (f.depends_on && changedKey in f.depends_on) {
                const fieldEl = document.getElementById(`cfg-field-${this._cssKey(f.key)}`);
                if (fieldEl) {
                    if (this._isDependencyHidden(f)) {
                        fieldEl.classList.add("hidden");
                    } else {
                        fieldEl.classList.remove("hidden");
                    }
                }
            }
        }
    }

    // ── Save / Reset ────────────────────────────────────────────

    /**
     * Save changed values for a section via PUT /api/config.
     * @param {string} section - Section key
     */
    async _saveSection(section) {
        const changes = this._getChangedValues(section);
        if (Object.keys(changes).length === 0) {
            this._showToast("No changes to save.", "info");
            return;
        }

        // Validate first
        try {
            const valRes = await fetch("/api/config/validate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(changes),
            });
            const valData = await valRes.json();
            if (!valData.valid && valData.errors && valData.errors.length > 0) {
                this._showValidationErrors(valData.errors);
                return;
            }
        } catch (err) {
            console.error("[ConfigurationManager] Validation failed:", err);
        }

        // Save
        try {
            const res = await fetch("/api/config/", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(changes),
            });

            if (!res.ok) {
                const errData = await res.json().catch(() => ({}));
                this._showToast(errData.error || `Save failed (${res.status})`, "error");
                return;
            }

            const result = await res.json();

            // Update originals for saved keys
            for (const k of Object.keys(changes)) {
                this.originalValues[k] = this.values[k];
            }

            // Clear changed markers
            document.querySelectorAll(".config-input.changed, .config-select.changed").forEach(el => {
                if (changes[el.dataset.key] !== undefined) {
                    el.classList.remove("changed");
                }
            });

            // Handle restart-required
            if (result.restart_required && result.restart_required.length > 0) {
                this.restartFields = [
                    ...new Set([...this.restartFields, ...result.restart_required]),
                ];
                this._showRestartBanner();
                this._showToast(`Saved. Restart required for: ${result.restart_required.length} field(s).`, "warning");
            } else {
                this._showToast("Configuration saved successfully.", "success");
            }
        } catch (err) {
            console.error("[ConfigurationManager] Save error:", err);
            this._showToast("Save failed: " + (err.message || err), "error");
        }
    }

    /**
     * Reset all fields in a section to their original (loaded) values.
     * @param {string} section - Section key
     */
    _resetSection(section) {
        const fields = section === "pv_forecast"
            ? Object.keys(this.values).filter(k => k.match(/^pv_forecast\.\d+\./))
            : this._fieldsForSection(section).map(f => f.key);

        for (const key of fields) {
            if (key in this.originalValues) {
                this.values[key] = JSON.parse(JSON.stringify(this.originalValues[key]));
            }
        }

        // Re-render section
        this._selectSection(section);
        this._showToast("Reset to last saved values.", "info");
    }

    /**
     * Get changed values for a section.
     * @param {string} section - Section key
     * @returns {Object} Changed key-value pairs
     */
    _getChangedValues(section) {
        const changes = {};

        if (section === "pv_forecast") {
            // For PV, capture all pv_forecast.N.field keys
            for (const [k, v] of Object.entries(this.values)) {
                if (/^pv_forecast\.\d+\./.test(k)) {
                    if (String(v) !== String(this.originalValues[k] ?? "")) {
                        changes[k] = v;
                    }
                }
            }
        } else {
            const fields = this._fieldsForSection(section);
            for (const f of fields) {
                const curr = this.values[f.key];
                const orig = this.originalValues[f.key];
                if (String(curr) !== String(orig)) {
                    changes[f.key] = curr;
                }
            }
        }

        return changes;
    }

    // ── Validation display ──────────────────────────────────────

    /**
     * Show validation errors on the form.
     * @param {Object[]} errors - Array of {key, error} objects
     */
    _showValidationErrors(errors) {
        // Clear previous errors
        document.querySelectorAll(".config-field-error.visible").forEach(el => {
            el.classList.remove("visible");
            el.textContent = "";
        });
        document.querySelectorAll(".config-input.invalid").forEach(el => {
            el.classList.remove("invalid");
        });

        for (const e of errors) {
            const errEl = document.getElementById(`cfg-err-${this._cssKey(e.key)}`);
            if (errEl) {
                errEl.textContent = e.error;
                errEl.classList.add("visible");
            }
            const input = document.querySelector(`[data-key="${e.key}"]`);
            if (input) {
                input.classList.add("invalid");
            }
        }

        this._showToast(`Validation failed: ${errors.length} error(s).`, "error");
    }

    // ── Restart banner ──────────────────────────────────────────

    /**
     * Show the restart-required banner in the content area.
     */
    _showRestartBanner() {
        const banner = document.getElementById("cfg-restart-banner");
        const msg = document.getElementById("cfg-restart-msg");
        if (banner) {
            banner.classList.add("visible");
        }
        if (msg) {
            msg.textContent = `Restart required for: ${this.restartFields.join(", ")}`;
        }
    }

    // ── Toast notifications ─────────────────────────────────────

    /**
     * Show a toast notification.
     * @param {string} message - Toast message
     * @param {string} type - 'info', 'success', 'warning', or 'error'
     * @param {number} duration - Auto-dismiss in ms (0 = no auto-dismiss)
     */
    _showToast(message, type = "info", duration = 3500) {
        this._ensureToastContainer();

        const styles = {
            info:    { border: "#1aa1f3", icon: "fa-circle-info",            color: "#1aa1f3" },
            success: { border: "#28a745", icon: "fa-check-circle",           color: "#28a745" },
            warning: { border: "#ffc107", icon: "fa-exclamation-circle",     color: "#ffc107" },
            error:   { border: "#dc3545", icon: "fa-exclamation-triangle",   color: "#dc3545" },
        };
        const s = styles[type] || styles.info;

        const toast = document.createElement("div");
        toast.style.cssText = `
            display:flex;align-items:center;gap:12px;
            background-color:rgba(59,59,59,0.99);border:2px solid ${s.border};
            border-radius:8px;padding:14px 18px;color:#e0e0e0;font-size:0.95em;
            font-weight:500;box-shadow:0 4px 12px rgba(0,0,0,0.3);pointer-events:auto;
            animation:slideIn 0.3s ease-out;max-width:350px;word-wrap:break-word;opacity:0.9;
        `;
        toast.innerHTML = `
            <i class="fas ${s.icon}" style="color:${s.color};flex-shrink:0;"></i>
            <span>${this._escapeHtml(message)}</span>
            <button style="background:none;border:none;color:#999;cursor:pointer;font-size:1.1em;
                           padding:0;margin-left:8px;flex-shrink:0;transition:color 0.2s;"
                    onmouseover="this.style.color='#e0e0e0'" onmouseout="this.style.color='#999'"
                    onclick="this.parentElement.remove()">&#x2715;</button>
        `;
        this.toastContainer.appendChild(toast);

        if (duration > 0) {
            setTimeout(() => {
                if (toast.parentElement) {
                    toast.style.animation = "slideOut 0.3s ease-out forwards";
                    setTimeout(() => toast.remove(), 300);
                }
            }, duration);
        }
    }

    /**
     * Ensure the toast container exists.
     */
    _ensureToastContainer() {
        if (this.toastContainer && document.body.contains(this.toastContainer)) {
            return;
        }
        let container = document.getElementById("config-toast-container");
        if (!container) {
            container = document.createElement("div");
            container.id = "config-toast-container";
            container.style.cssText = `
                position:fixed;top:20px;right:20px;z-index:10002;
                display:flex;flex-direction:column;gap:10px;pointer-events:none;
            `;
            document.body.appendChild(container);
        }
        this.toastContainer = container;
    }

    // ── Badge rendering ─────────────────────────────────────────

    /**
     * Render a label badge.
     * @param {string} label - Label tag
     * @returns {string} Badge HTML
     */
    _renderBadge(label) {
        switch (label) {
            case "experimental":
                return `<span class="config-badge config-badge-experimental">
                    <i class="fas fa-flask"></i> Experimental</span>`;
            case "deprecated":
                return `<span class="config-badge config-badge-deprecated">
                    <i class="fas fa-triangle-exclamation"></i> Deprecated</span>`;
            case "restart_required":
                return `<span class="config-badge config-badge-restart">
                    <i class="fas fa-rotate"></i> Restart</span>`;
            default:
                return "";
        }
    }

    // ── Help toggle ─────────────────────────────────────────────

    /**
     * Toggle inline help text for a field.
     * @param {string} key - Dot-notation key
     */
    _toggleHelp(key) {
        const el = document.getElementById(`cfg-help-${this._cssKey(key)}`);
        if (el) {
            el.classList.toggle("visible");
        }
    }

    /**
     * Toggle password field visibility.
     * @param {string} key - Dot-notation key
     */
    _togglePassword(key) {
        const input = document.getElementById(`cfg-pw-${this._cssKey(key)}`);
        if (input) {
            input.type = input.type === "password" ? "text" : "password";
        }
    }

    // ── Utility ─────────────────────────────────────────────────

    /**
     * Convert a dot-notation key to a CSS-safe id fragment.
     * @param {string} key - Dot-notation key
     * @returns {string} CSS-safe key
     */
    _cssKey(key) {
        return key.replace(/\./g, "-");
    }

    /**
     * Convert "battery.min_soc_percentage" to "Min Soc Percentage".
     * @param {string} key - Dot-notation key
     * @returns {string} Human-readable label
     */
    _prettyLabel(key) {
        const parts = key.split(".");
        const last = parts[parts.length - 1];
        return last
            .replace(/_/g, " ")
            .replace(/\b\w/g, c => c.toUpperCase());
    }

    /**
     * Get the schema default for a key.
     * @param {string} key - Dot-notation key
     * @returns {*} Default value
     */
    _getSchemaDefault(key) {
        if (!this.schema) {
            return undefined;
        }
        const f = this.schema.find(fd => fd.key === key);
        return f ? f.default : undefined;
    }

    /**
     * Escape HTML special characters.
     * @param {string} str - Input string
     * @returns {string} Escaped string
     */
    _escapeHtml(str) {
        if (!str) {
            return "";
        }
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    /**
     * Escape a string for use in an HTML attribute.
     * @param {string} str - Input string
     * @returns {string} Escaped string
     */
    _escapeAttr(str) {
        if (!str) {
            return "";
        }
        return str.replace(/&/g, "&amp;").replace(/"/g, "&quot;")
                  .replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }
}


// ── Global instance & show function ─────────────────────────────
let configurationManager;

/**
 * Show the Configuration overlay (called from menu).
 */
function showConfigurationMenu() {
    if (!configurationManager) {
        configurationManager = new ConfigurationManager();
    }
    configurationManager.showConfigurationMenu();
}
