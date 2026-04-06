/**
 * ConfigurationManager — Web Config UI for EOS Connect
 *
 * Fetches the config schema + current values from /api/config,
 * renders a section-based editor with level filtering, and
 * saves changes back via PUT /api/config.
 */

/* global showFullScreenOverlay, closeFullScreenOverlay, isMobile */

// ── Section metadata (icon + display name) ──────────────────────
// Populated from /api/config/schema at runtime (SPOT from schema.py).
// Fallback values used only when schema hasn't loaded yet.
let CONFIG_SECTIONS = {};
let SECTION_ORDER = [];  // Track explicit section order from API

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
        this._restartPollTimer = null; // interval ID for restart polling
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
            this._notifyRestartState();
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
            fetch("api/config/schema"),
            fetch("api/config/export"),
        ]);

        if (!schemaRes.ok) {
            throw new Error(`Schema: ${schemaRes.status}`);
        }
        if (!valuesRes.ok) {
            throw new Error(`Values: ${valuesRes.status}`);
        }

        const schemaData = await schemaRes.json();
        this.schema = schemaData.fields || schemaData;
        // Populate section metadata from schema (SPOT)
        if (schemaData.sections) {
            CONFIG_SECTIONS = schemaData.sections;
            console.log("[ConfigManager] Loaded sections order:", Object.keys(CONFIG_SECTIONS));
        }
        // Use explicit section order from API if available
        if (schemaData.section_order) {
            SECTION_ORDER = schemaData.section_order;
            console.log("[ConfigManager] Explicit section order from API:", SECTION_ORDER);
        }
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

        // Load any pending restart-required fields from the server
        try {
            const rrRes = await fetch("api/config/restart-required");
            if (rrRes.ok) {
                const rrData = await rrRes.json();
                if (rrData.fields && rrData.fields.length > 0) {
                    this.restartFields = rrData.fields;
                }
            }
        } catch (_) {
            // Ignore — non-critical
        }
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
            <div style="display:flex;align-items:center;gap:10px;flex:1;min-width:0;flex-wrap:wrap;">
                <button class="config-mobile-back config-btn config-btn-secondary"
                        onclick="configurationManager._mobileBack()"
                        style="padding:6px 10px;font-size:0.85em;">
                    <i class="fas fa-arrow-left"></i>
                </button>
                <i class="fas fa-gear" style="color:#cccccc;"></i>
                <span class="config-desktop-only">Configuration</span>
                <select class="config-level-select"
                        onchange="configurationManager._setLevel(this.value)"
                        title="Disclosure level">${opts}</select>
                <div style="margin-left:auto;display:flex;gap:6px;align-items:center;">
                    <button onclick="configurationManager._exportConfig()"
                            class="config-btn config-btn-secondary config-header-tool"
                            style="padding:5px 10px;font-size:0.8em;"
                            title="Export Configuration">
                        <i class="fas fa-download"></i>
                    </button>
                    <button onclick="document.getElementById('cfg-import-file').click()"
                            class="config-btn config-btn-secondary config-header-tool"
                            style="padding:5px 10px;font-size:0.8em;"
                            title="Import Configuration">
                        <i class="fas fa-upload"></i>
                    </button>
                    <input type="file" id="cfg-import-file" accept=".json"
                           style="display:none;"
                           onchange="configurationManager._importConfig(this.files[0])">
                    <button onclick="closeFullScreenOverlay(); showSetupWizard();"
                            style="background:#4a9eff;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-size:0.8em;cursor:pointer;display:inline-flex;align-items:center;gap:5px;"
                            title="Run Setup Wizard">
                        <i class="fas fa-wand-magic-sparkles"></i>
                        <span class="config-desktop-only">Wizard</span>
                    </button>
                </div>
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
     * Get the ordered list of section keys from SECTION_META.
     * Uses explicit section order from API if available, otherwise uses CONFIG_SECTIONS keys.
     * @returns {string[]} Section keys in correct order
     */
    _orderedSections() {
        // Prefer explicit order from API (section_order array)
        if (SECTION_ORDER && SECTION_ORDER.length > 0) {
            console.log("[ConfigManager] Using explicit SECTION_ORDER:", SECTION_ORDER);
            return SECTION_ORDER;
        }
        // Fallback to Object.keys order (should preserve insertion order in modern JS)
        const ordered = Object.keys(CONFIG_SECTIONS);
        console.log("[ConfigManager] Using Object.keys() order:", ordered);
        return ordered;
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

        // Show restart banner if fields pending from a previous save
        if (this.restartFields.length > 0) {
            this._showRestartBanner();
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

        // Unmet dependencies banner
        html += `<div class="config-unmet-deps-banner" id="cfg-unmet-deps-banner">
            <i class="fas fa-times-circle" style="color: #ffc107; margin-right: 12px;"></i>
            <div id="cfg-unmet-deps-content"></div>
        </div>`;

        html += `<div class="config-section-title">
            <i class="fa-solid ${meta.icon}" style="color:#4a9eff;"></i>
            ${meta.label}
        </div>`;

        for (const [groupName, groupFields] of groups) {
            if (groupName) {
                const allHidden = groupFields.every(f => this._isDependencyHidden(f));
                html += `<div class="config-group${allHidden ? ' hidden' : ''}" data-group="${groupName}">
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
            ? `<button class="config-help-btn" tabindex="-1" onclick="configurationManager._toggleHelp('${f.key}')" title="Help">
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
        const changedCls = this._isChanged(f.key) ? " changed" : "";
        const maxLen = inputType === "text" ? ` maxlength="2000"` : "";
        return `<input class="config-input${changedCls}" type="${inputType}"
                       data-key="${f.key}"
                       value="${this._escapeAttr(String(displayVal))}"
                       ${attrs}${maxLen}
                       oninput="configurationManager._onFieldChange('${f.key}', this.value)"
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
            
            // Conditional disabling for specific fields
            let disabled = "";
            let title = "";
            let displayLabel = c;  // Label to show in dropdown
            
            // Disable "evcc" option in pv_forecast_source.source if evcc.url is not configured
            if (f.key === "pv_forecast_source.source" && String(c) === "evcc") {
                const evccUrl = this.values["evcc.url"] || "http://yourEVCCserver:7070";
                // Check if URL is at default or empty
                const isDefault = evccUrl.trim() === "" || evccUrl === "http://yourEVCCserver:7070";
                if (isDefault) {
                    disabled = "disabled";
                    title = "title='Configure EVCC URL first'";
                    displayLabel = `${c} (not available)`;
                }
            }
            
            // Disable "evcc" option in inverter.type if evcc.url is not configured
            if (f.key === "inverter.type" && String(c) === "evcc") {
                const evccUrl = this.values["evcc.url"] || "http://yourEVCCserver:7070";
                // Check if URL is at default or empty
                const isDefault = evccUrl.trim() === "" || evccUrl === "http://yourEVCCserver:7070";
                if (isDefault) {
                    disabled = "disabled";
                    title = "title='Configure EVCC URL first'";
                    displayLabel = `${c} (not available)`;
                }
            }
            
            return `<option value="${this._escapeAttr(String(c))}" ${selected} ${disabled} ${title} style="${disabled ? 'color: #888; font-style: italic;' : ''}">${displayLabel}</option>`;
        }).join("");
        const changedCls = this._isChanged(f.key) ? " changed" : "";
        return `<select class="config-select${changedCls}" data-key="${f.key}"
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
        const checked = val === true || val === "true" || val === "True"
            || val === "enabled" || val === "yes" || val === "1" ? "checked" : "";
        const changedCls = this._isChanged(f.key) ? " changed" : "";
        return `<label class="config-toggle${changedCls}" data-toggle-key="${f.key}">
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
        const changedCls = this._isChanged(f.key) ? " changed" : "";
        return `<div class="config-password-wrap">
            <input class="config-input${changedCls}" type="password"
                   id="cfg-pw-${this._cssKey(f.key)}"
                   data-key="${f.key}"
                   value="${this._escapeAttr(String(displayVal))}"
                   oninput="configurationManager._onFieldChange('${f.key}', this.value)"
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
     * Add a new PV installation.
     * If installations exist, copy values from the last one.
     * Otherwise use schema defaults.
     * New fields are NOT added to originalValues so they appear as "changed".
     */
    _addPvInstallation() {
        const installations = this._getPvInstallations();
        const newIdx = installations.length;
        const pvFields = this.schema.filter(f => f.section === "pv_forecast");
        
        // Get the last installation to use as template (if exists)
        const lastInstallation = installations.length > 0 ? installations[installations.length - 1] : null;

        for (const f of pvFields) {
            const subKey = f.key.split(".").pop();
            const fullKey = `pv_forecast.${newIdx}.${subKey}`;
            let value;

            if (subKey === "name") {
                // Auto-generate name: copy last name and append index, or use default
                if (lastInstallation && lastInstallation.name) {
                    // Find a unique name by appending number
                    let baseName = lastInstallation.name.replace(/\d+$/, ""); // Remove trailing numbers
                    let counter = newIdx + 1;
                    value = `${baseName}${counter}`;
                    // Ensure uniqueness
                    while (installations.some(inst => inst.name === value)) {
                        counter++;
                        value = `${baseName}${counter}`;
                    }
                } else {
                    // Use default numbering
                    value = `myPvInstallation${newIdx + 1}`;
                }
            } else if (lastInstallation && subKey in lastInstallation) {
                // Copy value from last installation (except name)
                value = lastInstallation[subKey];
            } else {
                // Use schema default
                value = f.default;
            }

            this.values[fullKey] = value;
            // DO NOT add to originalValues — this marks all fields as "changed"
            // which ensures they will be sent to the API on save
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
            <button class="config-btn config-btn-secondary config-actions-tool"
                    onclick="configurationManager._exportConfig()"
                    title="Export Configuration">
                <i class="fas fa-download"></i>
            </button>
            <button class="config-btn config-btn-secondary config-actions-tool"
                    onclick="document.getElementById('cfg-import-file').click()"
                    title="Import Configuration">
                <i class="fas fa-upload"></i>
            </button>
        </div>`;
    }

    // ── Field change handling ───────────────────────────────────

    /**
     * Check if a field value differs from its original (loaded) value.
     * @param {string} key - Dot-notation key
     * @returns {boolean} True if changed
     */
    _isChanged(key) {
        return String(this.values[key] ?? "") !== String(this.originalValues[key] ?? "");
    }

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
        const input = document.querySelector(`input[data-key="${key}"], select[data-key="${key}"]`);
        if (input && input.classList) {
            const original = this.originalValues[key];
            if (String(value) !== String(original)) {
                input.classList.add("changed");
            } else {
                input.classList.remove("changed");
            }
        }
        // For toggles, also mark/unmark the visible <label> wrapper
        const toggleLabel = document.querySelector(`label[data-toggle-key="${key}"]`);
        if (toggleLabel) {
            const original = this.originalValues[key];
            if (String(value) !== String(original)) {
                toggleLabel.classList.add("changed");
            } else {
                toggleLabel.classList.remove("changed");
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
        this._updateGroupVisibility();
    }

    /**
     * Hide group containers when all their child fields are hidden.
     */
    _updateGroupVisibility() {
        document.querySelectorAll(".config-group[data-group]").forEach(groupEl => {
            const fields = groupEl.querySelectorAll(".config-field");
            const allHidden = fields.length > 0 && [...fields].every(f => f.classList.contains("hidden"));
            if (allHidden) {
                groupEl.classList.add("hidden");
            } else {
                groupEl.classList.remove("hidden");
            }
        });
    }

    // ── Save / Reset ────────────────────────────────────────────

    /**
     * Save changed values for a section via PUT /api/config.
     * @param {string} section - Section key
     */
    async _saveSection(section) {
        // Clear any previous validation errors
        this._clearValidationErrors();

        const changes = this._getChangedValues(section);
        if (Object.keys(changes).length === 0) {
            this._showToast("No changes to save.", "info");
            return;
        }

        // --- Client-side pre-validation ---
        const clientErrors = [];
        const emptyPasswords = [];

        for (const [key, value] of Object.entries(changes)) {
            const fd = this.schema.find(f => f.key === key);
            if (!fd) continue;

            if (typeof value === "string") {
                if (value.length > 2000) {
                    clientErrors.push({
                        key,
                        error: `Value too long (${value.length} chars, max 2000)`,
                    });
                } else if ((fd.type === "str" || fd.type === "sensor")
                            && /<[a-zA-Z/!]/.test(value)) {
                    clientErrors.push({
                        key,
                        error: "HTML tags are not allowed in this field",
                    });
                }
            }

            if (fd.type === "password" && value === "") {
                emptyPasswords.push(key);
            }
        }

        if (clientErrors.length > 0) {
            this._showValidationErrors(clientErrors);
            return;
        }

        if (emptyPasswords.length > 0) {
            // Show inline warning per field (non-blocking)
            for (const key of emptyPasswords) {
                const errEl = document.getElementById(`cfg-err-${this._cssKey(key)}`);
                if (errEl) {
                    errEl.textContent = "Saved empty — ensure no credentials are required here.";
                    errEl.classList.add("visible", "warning");
                }
            }
            this._showToast(
                `${emptyPasswords.length} password field(s) saved empty.`,
                "warning",
            );
        }

        // Validate first
        try {
            const valRes = await fetch("api/config/validate", {
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
            const res = await fetch("api/config/", {
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

            // Handle unmet dependencies (blocking condition)
            if (!result.success && result.unmet_dependencies && result.unmet_dependencies.length > 0) {
                this._showUnmetDependencies(result.unmet_dependencies);
                return;
            }

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
            document.querySelectorAll(".config-toggle.changed").forEach(el => {
                if (changes[el.dataset.toggleKey] !== undefined) {
                    el.classList.remove("changed");
                }
            });

            // Handle restart-required vs hot-reloaded
            const hasRestart = result.restart_required && result.restart_required.length > 0;
            const hasHotReload = result.hot_reloaded && result.hot_reloaded.length > 0;

            if (hasRestart) {
                this.restartFields = [
                    ...new Set([...this.restartFields, ...result.restart_required]),
                ];
                this._showRestartBanner();
                this._notifyRestartState();
                this._showToast(`Saved. Restart required for: ${result.restart_required.length} field(s).`, "warning");
            } else if (hasHotReload) {
                this._showToast(`Saved & applied live (${result.hot_reloaded.length} field(s)). No restart needed.`, "success");
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

    // ── Import / Export ─────────────────────────────────────────

    /**
     * Export the full configuration as a downloadable JSON file.
     */
    async _exportConfig() {
        try {
            const res = await fetch("api/config/export");
            if (!res.ok) {
                this._showToast("Export failed: " + res.status, "error");
                return;
            }
            const data = await res.json();
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `eos_connect_config_${new Date().toISOString().slice(0, 10)}.json`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            this._showToast("Configuration exported.", "success");
        } catch (err) {
            this._showToast("Export failed: " + (err.message || err), "error");
        }
    }

    /**
     * Import configuration from a JSON file.
     * @param {File} file - The selected JSON file
     */
    async _importConfig(file) {
        if (!file) {
            return;
        }
        try {
            const text = await file.text();
            const data = JSON.parse(text);
            if (typeof data !== "object" || Array.isArray(data)) {
                this._showToast("Import failed: file must contain a JSON object.", "error");
                return;
            }

            const res = await fetch("api/config/import", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(data),
            });

            const result = await res.json();
            if (!res.ok) {
                const errMsg = result.error || result.errors?.map(e => e.error).join(", ") || res.status;
                this._showToast("Import failed: " + errMsg, "error");
                return;
            }

            const count = result.imported || 0;
            const skipped = result.skipped || 0;
            let msg = `Imported ${count} setting(s).`;
            if (skipped > 0) {
                msg += ` Skipped ${skipped} unknown key(s).`;
            }

            // Reload data to reflect imported values
            await this._loadData();
            this._selectSection(this.currentSection || this._orderedSections()[0]);
            this._showToast(msg, "success");
        } catch (err) {
            this._showToast("Import failed: " + (err.message || err), "error");
        }
        // Reset file input so the same file can be re-imported
        const fileInput = document.getElementById("cfg-import-file");
        if (fileInput) {
            fileInput.value = "";
        }
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
    /**
     * Clear all validation error indicators from the form.
     */
    _clearValidationErrors() {
        document.querySelectorAll(".config-field-error.visible").forEach(el => {
            el.classList.remove("visible", "warning");
            el.textContent = "";
        });
        document.querySelectorAll(".config-input.invalid").forEach(el => {
            el.classList.remove("invalid");
        });
    }

    _showValidationErrors(errors) {
        this._clearValidationErrors();

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

    /**
     * Notify external UI about restart-pending state.
     * Shows an orange dot on the hamburger menu when restart is needed.
     */
    _notifyRestartState() {
        if (typeof MenuNotifications !== "undefined") {
            MenuNotifications.setRestartPending(this.restartFields.length > 0);
        }
        // Start polling to auto-clear after server restart
        this._startRestartPoll();
    }

    /**
     * Poll /api/config/restart-required to detect when server has restarted
     * (which clears the pending list). Clears banner + dot when empty.
     */
    _startRestartPoll() {
        if (this._restartPollTimer) {
            clearInterval(this._restartPollTimer);
        }
        if (this.restartFields.length === 0) {
            return;
        }
        this._restartPollTimer = setInterval(async () => {
            try {
                const res = await fetch("api/config/restart-required");
                if (!res.ok) {
                    return;
                }
                const data = await res.json();
                if (!data.fields || data.fields.length === 0) {
                    // Server has restarted — clear banner and dot
                    this.restartFields = [];
                    clearInterval(this._restartPollTimer);
                    this._restartPollTimer = null;
                    const banner = document.getElementById("cfg-restart-banner");
                    if (banner) {
                        banner.classList.remove("visible");
                    }
                    if (typeof MenuNotifications !== "undefined") {
                        MenuNotifications.setRestartPending(false);
                    }
                    this._showToast("Server restarted. Changes applied.", "success");
                }
            } catch (_) {
                // Server might be restarting — ignore
            }
        }, 10000); // Check every 10 seconds
    }

    /**
     * Display unmet dependencies banner with details and links to required fields.
     * @param {Array} dependencies - List of {field, reason, requires} objects
     */
    _showUnmetDependencies(dependencies) {
        const banner = document.getElementById("cfg-unmet-deps-banner");
        const content = document.getElementById("cfg-unmet-deps-content");
        
        if (banner && content) {
            let html = `
                <div style="color: #ffc107; font-weight: bold; margin-bottom: 12px;">
                    <i class="fas fa-exclamation-triangle"></i> Cannot save: required settings not configured
                </div>
                <ul style="margin: 0; padding-left: 20px; font-size: 0.9em;">
            `;
            for (const dep of dependencies) {
                html += `<li style="margin-bottom: 8px;">
                    <strong>${dep.field}</strong>: ${dep.reason}
                    <br><small style="color: #bbb;">Requires: <strong>${dep.requires}</strong></small>
                </li>`;
            }
            html += `</ul>`;
            
            content.innerHTML = html;
            banner.classList.add("visible");
            this._showToast("Cannot save: required dependencies not configured", "error");
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

/**
 * On page load, check if a restart is pending and show the dot immediately.
 * This ensures the notification is visible after page reload without
 * requiring the config overlay to be opened first.
 */
document.addEventListener("DOMContentLoaded", () => {
    fetch("api/config/restart-required")
        .then(r => r.ok ? r.json() : null)
        .then(data => {
            if (data && data.fields && data.fields.length > 0) {
                if (!configurationManager) {
                    configurationManager = new ConfigurationManager();
                }
                configurationManager.restartFields = data.fields;
                if (typeof MenuNotifications !== "undefined") {
                    MenuNotifications.setRestartPending(true);
                }
                configurationManager._startRestartPoll();
            }
        })
        .catch(() => {}); // non-critical
});
