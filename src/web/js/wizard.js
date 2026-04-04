/**
 * SetupWizard — Step-by-step initial configuration for EOS Connect.
 *
 * Guides new users through essential (getting_started level) settings
 * using a multi-step overlay wizard. Saves values via PUT /api/config
 * and marks the wizard as completed when finished.
 */

/* global showFullScreenOverlay, closeFullScreenOverlay, CONFIG_SECTIONS */

class SetupWizard {
    /**
     * Define the wizard steps and their associated schema sections/fields.
     */
    constructor() {
        this.schema = null;
        this.values = {};
        this.currentStep = 0;
        this.skippedSteps = new Set();

        /**
         * Each step maps to one or more schema sections.
         * Only getting_started-level fields are shown.
         */
        this.steps = [
            {
                id: "welcome",
                title: "Welcome to EOS Connect",
                icon: "fa-plug-circle-bolt",
                sections: [],
                description: "",
            },
            {
                id: "data_source",
                title: "Data Source",
                icon: "fa-plug",
                sections: ["data_source"],
                description: "Connect to Home Assistant or OpenHAB to read sensor data.",
            },
            {
                id: "eos",
                title: "Optimizer",
                icon: "fa-server",
                sections: ["eos"],
                description: "Select and configure the optimization backend.",
            },
            {
                id: "battery",
                title: "Battery",
                icon: "fa-battery-full",
                sections: ["battery"],
                description: "Set your battery capacity and SOC limits.",
            },
            {
                id: "price",
                title: "Pricing",
                icon: "fa-coins",
                sections: ["price"],
                description: "Choose your electricity price provider.",
            },
            {
                id: "pv",
                title: "PV Forecast",
                icon: "fa-solar-panel",
                sections: ["pv_forecast_source", "pv_forecast"],
                description: "Configure your solar forecast provider and PV installation.",
            },
            {
                id: "inverter",
                title: "Inverter",
                icon: "fa-microchip",
                sections: ["inverter"],
                description: "Select your inverter for battery charge control.",
            },
            {
                id: "load",
                title: "Load Sensor",
                icon: "fa-bolt",
                sections: ["load"],
                description: "Set the sensor entity for household load data.",
            },
            {
                id: "review",
                title: "Review & Finish",
                icon: "fa-check-circle",
                sections: [],
                description: "",
            },
        ];
    }

    // ── Public entry ────────────────────────────────────────────

    /**
     * Launch the setup wizard overlay.
     */
    async show() {
        // Hide the startup error/loading overlay so the wizard is fully visible
        const startupOverlay = document.getElementById("overlay");
        if (startupOverlay) {
            startupOverlay.style.display = "none";
        }

        showFullScreenOverlay("Setup Wizard", `
            <div style="display:flex;justify-content:center;align-items:center;height:100%;color:#888;">
                <i class="fas fa-spinner fa-spin" style="font-size:2em;margin-right:12px;"></i>
                Loading setup wizard…
            </div>
        `);

        try {
            await this._loadData();
            this.currentStep = 0;
            this._render();
        } catch (err) {
            console.error("[SetupWizard] Failed to load:", err);
            document.getElementById("full_screen_content").innerHTML = `
                <div style="text-align:center;padding:40px;color:#dc3545;">
                    <i class="fas fa-exclamation-triangle" style="font-size:2em;margin-bottom:12px;"></i>
                    <p>Failed to load wizard data</p>
                    <p style="font-size:0.85em;color:#888;">${this._escapeHtml(String(err.message || err))}</p>
                </div>
            `;
        }
    }

    // ── Data ────────────────────────────────────────────────────

    /**
     * Fetch schema and current values.
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
        // Update section metadata from schema (SPOT)
        if (schemaData.sections) {
            CONFIG_SECTIONS = schemaData.sections;
        }
        const raw = await valuesRes.json();
        this.values = {};
        for (const [k, v] of Object.entries(raw)) {
            this.values[k] = v;
        }
        // Fill defaults for missing keys
        for (const f of this.schema) {
            if (!(f.key in this.values)) {
                this.values[f.key] = f.default;
            }
        }
    }

    // ── Rendering ───────────────────────────────────────────────

    /**
     * Render the current wizard step into the overlay.
     */
    _render() {
        const step = this.steps[this.currentStep];
        const totalSteps = this.steps.length;

        const html = `
            <div class="wizard-container">
                ${this._renderProgress(totalSteps)}
                <div class="wizard-step" id="wizard-step-content">
                    ${this._renderStepContent(step)}
                </div>
                ${this._renderNav(step)}
            </div>
        `;

        const header = `<i class="fas ${step.icon}" style="margin-right:8px;"></i> Setup Wizard`;
        showFullScreenOverlay(header, html);
        this._bindStepEvents(step);
    }

    /**
     * Render the progress bar.
     * @param {number} total - Total number of steps
     * @returns {string} HTML string
     */
    _renderProgress(total) {
        let bars = "";
        for (let i = 0; i < total; i++) {
            let cls = "";
            if (i < this.currentStep) {
                cls = "completed";
            } else if (i === this.currentStep) {
                cls = "active";
            }
            bars += `<div class="wizard-progress-step ${cls}"></div>`;
        }
        const step = this.steps[this.currentStep];
        return `
            <div class="wizard-progress">${bars}</div>
            <div class="wizard-step-label">Step ${this.currentStep + 1} of ${total} — ${this._escapeHtml(step.title)}</div>
        `;
    }

    /**
     * Render the content area for a step.
     * @param {object} step - Step definition
     * @returns {string} HTML string
     */
    _renderStepContent(step) {
        if (step.id === "welcome") {
            return this._renderWelcome();
        }
        if (step.id === "review") {
            return this._renderReview();
        }
        return this._renderFields(step);
    }

    /**
     * Render the welcome step.
     * @returns {string} HTML string
     */
    _renderWelcome() {
        return `
            <div class="wizard-welcome">
                <div class="wizard-welcome-icon"><i class="fas fa-plug-circle-bolt"></i></div>
                <h2>Welcome to EOS Connect</h2>
                <p>This wizard will guide you through the essential settings to get your
                energy optimization system running. You can always change these later
                in the full Configuration menu.</p>
                <div class="wizard-welcome-features">
                    <div class="wizard-feature"><i class="fas fa-plug"></i><span>Data Source</span></div>
                    <div class="wizard-feature"><i class="fas fa-server"></i><span>Optimizer</span></div>
                    <div class="wizard-feature"><i class="fas fa-battery-full"></i><span>Battery</span></div>
                    <div class="wizard-feature"><i class="fas fa-coins"></i><span>Pricing</span></div>
                    <div class="wizard-feature"><i class="fas fa-solar-panel"></i><span>PV Forecast</span></div>
                    <div class="wizard-feature"><i class="fas fa-microchip"></i><span>Inverter</span></div>
                </div>
            </div>
        `;
    }

    /**
     * Render fields for a configuration step.
     * @param {object} step - Step definition
     * @returns {string} HTML string
     */
    _renderFields(step) {
        const fields = this._getStepFields(step);
        if (fields.length === 0) {
            return `<p style="color:#888;">No fields to configure for this step.</p>`;
        }

        let html = `
            <div class="wizard-step-title">${this._escapeHtml(step.title)}</div>
            <div class="wizard-step-description">${this._escapeHtml(step.description)}</div>
        `;

        for (const f of fields) {
            html += this._renderField(f);
        }
        return html;
    }

    /**
     * Render a single form field.
     * @param {object} f - Field definition from schema
     * @returns {string} HTML string
     */
    _renderField(f) {
        const value = this.values[f.key] !== undefined ? this.values[f.key] : f.default;
        const cssKey = f.key.replace(/\./g, "-");
        const label = this._prettyLabel(f.key);
        const conditionalClass = f.depends_on ? "wizard-conditional" : "";
        const hiddenClass = f.depends_on && !this._isDependencyMet(f) ? " hidden" : "";

        let inputHtml = "";
        switch (f.type) {
            case "select":
                inputHtml = this._renderSelect(f, value, cssKey);
                break;
            case "password":
                inputHtml = this._renderPassword(f, value, cssKey);
                break;
            case "bool":
                inputHtml = this._renderBool(f, value, cssKey);
                break;
            case "int":
            case "float":
                inputHtml = this._renderNumber(f, value, cssKey);
                break;
            default:
                inputHtml = this._renderText(f, value, cssKey);
                break;
        }

        return `
            <div class="wizard-field ${conditionalClass}${hiddenClass}" id="wiz-field-${cssKey}" data-key="${this._escapeAttr(f.key)}">
                <label>${this._escapeHtml(label)}${this._isRequired(f) ? ' <span class="required">*</span>' : ""}</label>
                ${inputHtml}
                <div class="field-hint">${this._escapeHtml(f.description)}</div>
                <div class="field-error" id="wiz-err-${cssKey}"></div>
            </div>
        `;
    }

    /**
     * Render a select dropdown.
     * @param {object} f - Field definition
     * @param {*} value - Current value
     * @param {string} cssKey - CSS-safe key
     * @returns {string} HTML string
     */
    _renderSelect(f, value, cssKey) {
        const choices = (f.validation && f.validation.choices) || [];
        let opts = "";
        for (const c of choices) {
            const sel = String(c) === String(value) ? " selected" : "";
            opts += `<option value="${this._escapeAttr(String(c))}"${sel}>${this._escapeHtml(String(c))}</option>`;
        }
        return `<select id="wiz-${cssKey}" data-key="${this._escapeAttr(f.key)}">${opts}</select>`;
    }

    /**
     * Render a password field with toggle.
     * @param {object} f - Field definition
     * @param {*} value - Current value
     * @param {string} cssKey - CSS-safe key
     * @returns {string} HTML string
     */
    _renderPassword(f, value, cssKey) {
        return `
            <div class="wizard-password-wrap">
                <input type="password" id="wiz-${cssKey}" data-key="${this._escapeAttr(f.key)}"
                       value="${this._escapeAttr(String(value || ""))}" autocomplete="off">
                <button type="button" class="wizard-password-toggle" data-target="wiz-${cssKey}" title="Toggle visibility">
                    <i class="fas fa-eye"></i>
                </button>
            </div>
        `;
    }

    /**
     * Render a boolean toggle (checkbox styled).
     * @param {object} f - Field definition
     * @param {*} value - Current value
     * @param {string} cssKey - CSS-safe key
     * @returns {string} HTML string
     */
    _renderBool(f, value, cssKey) {
        const checked = value ? " checked" : "";
        return `
            <label class="wizard-skip-row" style="cursor:pointer;margin:0;">
                <input type="checkbox" id="wiz-${cssKey}" data-key="${this._escapeAttr(f.key)}"${checked}>
                <span>${value ? "Enabled" : "Disabled"}</span>
            </label>
        `;
    }

    /**
     * Render a number input.
     * @param {object} f - Field definition
     * @param {*} value - Current value
     * @param {string} cssKey - CSS-safe key
     * @returns {string} HTML string
     */
    _renderNumber(f, value, cssKey) {
        const v = f.validation || {};
        const minAttr = v.min !== undefined ? ` min="${v.min}"` : "";
        const maxAttr = v.max !== undefined ? ` max="${v.max}"` : "";
        const step = f.type === "float" ? ' step="any"' : ' step="1"';
        return `<input type="number" id="wiz-${cssKey}" data-key="${this._escapeAttr(f.key)}"
                       value="${this._escapeAttr(String(value))}"${minAttr}${maxAttr}${step}>`;
    }

    /**
     * Render a text input.
     * @param {object} f - Field definition
     * @param {*} value - Current value
     * @param {string} cssKey - CSS-safe key
     * @returns {string} HTML string
     */
    _renderText(f, value, cssKey) {
        return `<input type="text" id="wiz-${cssKey}" data-key="${this._escapeAttr(f.key)}"
                       value="${this._escapeAttr(String(value || ""))}">`;
    }

    // ── Review step ─────────────────────────────────────────────

    /**
     * Render the review/summary step.
     * @returns {string} HTML string
     */
    _renderReview() {
        let html = `
            <div class="wizard-step-title">Review Your Settings</div>
            <div class="wizard-step-description">
                Here is a summary of your configuration. Click "Finish" to save and start,
                or go back to make changes.
            </div>
        `;

        for (let i = 1; i < this.steps.length - 1; i++) {
            const step = this.steps[i];
            const fields = this._getStepFields(step);
            if (fields.length === 0) {
                continue;
            }

            const sectionMeta = CONFIG_SECTIONS[step.sections[0]] || {};
            const icon = sectionMeta.icon || step.icon;

            html += `<div class="wizard-review-section">
                <h4><i class="fas ${icon}"></i> ${this._escapeHtml(step.title)}</h4>`;

            for (const f of fields) {
                if (f.depends_on && !this._isDependencyMet(f)) {
                    continue;
                }
                const val = this.values[f.key];
                const display = f.type === "password"
                    ? (val ? "••••••••" : "<em>not set</em>")
                    : this._escapeHtml(String(val !== undefined ? val : f.default));
                const valClass = f.type === "password" ? ' class="value password"' : ' class="value"';

                html += `<div class="wizard-review-row">
                    <span class="label">${this._escapeHtml(this._prettyLabel(f.key))}</span>
                    <span${valClass}>${display}</span>
                </div>`;
            }
            html += `</div>`;
        }

        return html;
    }

    // ── Navigation ──────────────────────────────────────────────

    /**
     * Render the bottom navigation bar.
     * @param {object} step - Current step definition
     * @returns {string} HTML string
     */
    _renderNav(step) {
        const isFirst = this.currentStep === 0;
        const isLast = this.currentStep === this.steps.length - 1;

        let backBtn = "";
        if (!isFirst) {
            backBtn = `<button class="wizard-btn wizard-btn-back" id="wiz-back">
                <i class="fas fa-arrow-left"></i> Back
            </button>`;
        } else {
            backBtn = `<div></div>`;
        }

        let nextBtn = "";
        if (isLast) {
            nextBtn = `<button class="wizard-btn wizard-btn-next" id="wiz-next">
                <i class="fas fa-check"></i> Finish
            </button>`;
        } else if (isFirst) {
            nextBtn = `<button class="wizard-btn wizard-btn-next" id="wiz-next">
                Let's Go <i class="fas fa-arrow-right"></i>
            </button>`;
        } else {
            nextBtn = `<button class="wizard-btn wizard-btn-next" id="wiz-next">
                Next <i class="fas fa-arrow-right"></i>
            </button>`;
        }

        return `<div class="wizard-nav">${backBtn}${nextBtn}</div>`;
    }

    // ── Event binding ───────────────────────────────────────────

    /**
     * Bind events for the current step after render.
     * @param {object} step - Current step definition
     */
    _bindStepEvents(step) {
        // Navigation buttons
        const backBtn = document.getElementById("wiz-back");
        if (backBtn) {
            backBtn.addEventListener("click", () => this._goBack());
        }
        const nextBtn = document.getElementById("wiz-next");
        if (nextBtn) {
            nextBtn.addEventListener("click", () => this._goNext());
        }

        // Password toggles
        document.querySelectorAll(".wizard-password-toggle").forEach(btn => {
            btn.addEventListener("click", () => {
                const inputId = btn.getAttribute("data-target");
                const input = document.getElementById(inputId);
                if (!input) {
                    return;
                }
                const icon = btn.querySelector("i");
                if (input.type === "password") {
                    input.type = "text";
                    icon.className = "fas fa-eye-slash";
                } else {
                    input.type = "password";
                    icon.className = "fas fa-eye";
                }
            });
        });

        // Checkbox label update
        document.querySelectorAll('.wizard-field input[type="checkbox"]').forEach(cb => {
            cb.addEventListener("change", () => {
                const span = cb.parentElement.querySelector("span");
                if (span) {
                    span.textContent = cb.checked ? "Enabled" : "Disabled";
                }
            });
        });

        // Select/input change → update local values + conditional visibility
        const container = document.getElementById("wizard-step-content");
        if (container) {
            container.addEventListener("change", (e) => {
                const el = e.target;
                const key = el.getAttribute("data-key");
                if (!key) {
                    return;
                }
                this._collectFieldValue(el, key);
                this._updateConditionalFields();
            });
            container.addEventListener("input", (e) => {
                const el = e.target;
                const key = el.getAttribute("data-key");
                if (!key) {
                    return;
                }
                this._collectFieldValue(el, key);
            });
        }
    }

    /**
     * Collect a field's current DOM value into this.values.
     * @param {HTMLElement} el - Input element
     * @param {string} key - Dot-notation key
     */
    _collectFieldValue(el, key) {
        const fieldDef = this.schema.find(f => f.key === key);
        if (!fieldDef) {
            return;
        }
        if (el.type === "checkbox") {
            this.values[key] = el.checked;
        } else if (fieldDef.type === "int") {
            this.values[key] = el.value === "" ? fieldDef.default : parseInt(el.value, 10);
        } else if (fieldDef.type === "float") {
            this.values[key] = el.value === "" ? fieldDef.default : parseFloat(el.value);
        } else {
            this.values[key] = el.value;
        }
    }

    /**
     * Update conditional field visibility based on current values.
     */
    _updateConditionalFields() {
        document.querySelectorAll(".wizard-conditional").forEach(div => {
            const key = div.getAttribute("data-key");
            const fieldDef = this.schema.find(f => f.key === key);
            if (!fieldDef || !fieldDef.depends_on) {
                return;
            }
            if (this._isDependencyMet(fieldDef)) {
                div.classList.remove("hidden");
            } else {
                div.classList.add("hidden");
            }
        });
    }

    // ── Navigation logic ────────────────────────────────────────

    /**
     * Go to the previous step.
     */
    _goBack() {
        if (this.currentStep > 0) {
            this._collectCurrentStep();
            this.currentStep--;
            this._render();
        }
    }

    /**
     * Validate current step and go to the next step, or finish.
     */
    async _goNext() {
        this._collectCurrentStep();

        // Validate current step fields
        const step = this.steps[this.currentStep];
        if (step.id !== "welcome" && step.id !== "review") {
            if (!this._validateStep(step)) {
                return;
            }
        }

        if (this.currentStep === this.steps.length - 1) {
            // Finish — save all and mark complete
            await this._finish();
        } else {
            this.currentStep++;
            this._render();
        }
    }

    /**
     * Collect all field values from the current step's DOM into this.values.
     */
    _collectCurrentStep() {
        const container = document.getElementById("wizard-step-content");
        if (!container) {
            return;
        }
        container.querySelectorAll("[data-key]").forEach(el => {
            const key = el.getAttribute("data-key");
            if (key) {
                this._collectFieldValue(el, key);
            }
        });
    }

    /**
     * Validate all visible fields in the current step.
     * @param {object} step - Step definition
     * @returns {boolean} True if valid
     */
    _validateStep(step) {
        const fields = this._getStepFields(step);
        let valid = true;

        for (const f of fields) {
            if (f.depends_on && !this._isDependencyMet(f)) {
                continue;
            }
            const cssKey = f.key.replace(/\./g, "-");
            const fieldDiv = document.getElementById(`wiz-field-${cssKey}`);
            const errDiv = document.getElementById(`wiz-err-${cssKey}`);
            if (!fieldDiv || !errDiv) {
                continue;
            }

            const value = this.values[f.key];
            const error = this._validateField(f, value);

            if (error) {
                fieldDiv.classList.add("has-error");
                errDiv.textContent = error;
                valid = false;
            } else {
                fieldDiv.classList.remove("has-error");
                errDiv.textContent = "";
            }
        }
        return valid;
    }

    /**
     * Validate a single field value.
     * @param {object} f - Field definition
     * @param {*} value - Current value
     * @returns {string} Error message or empty string
     */
    _validateField(f, value) {
        const v = f.validation || {};

        if (v.choices && v.choices.length > 0) {
            if (!v.choices.map(String).includes(String(value))) {
                return `Must be one of: ${v.choices.join(", ")}`;
            }
        }
        if (v.min !== undefined && typeof value === "number") {
            if (value < v.min) {
                return `Must be at least ${v.min}`;
            }
        }
        if (v.max !== undefined && typeof value === "number") {
            if (value > v.max) {
                return `Must be at most ${v.max}`;
            }
        }
        if (v.pattern && typeof value === "string") {
            try {
                if (!new RegExp(v.pattern).test(value)) {
                    return `Invalid format`;
                }
            } catch {
                // skip broken regex
            }
        }
        return "";
    }

    // ── Finish / Save ───────────────────────────────────────────

    /**
     * Save all wizard values and mark the wizard as completed.
     */
    async _finish() {
        const nextBtn = document.getElementById("wiz-next");
        if (nextBtn) {
            nextBtn.disabled = true;
            nextBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Saving…';
        }

        try {
            // Build payload — only getting_started fields that differ from defaults
            const payload = {};
            for (const f of this.schema) {
                if (f.level !== "getting_started") {
                    continue;
                }
                const val = this.values[f.key];
                if (val !== undefined) {
                    payload[f.key] = val;
                }
            }

            // Save config
            const saveRes = await fetch("api/config/", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!saveRes.ok) {
                const err = await saveRes.json().catch(() => ({}));
                throw new Error(err.error || `Save failed: ${saveRes.status}`);
            }

            // Mark wizard complete
            await fetch("api/config/wizard-complete", { method: "POST" });

            // Show success with restart instruction — don't auto-close,
            // because the server needs a restart to pick up the new config.
            document.getElementById("full_screen_content").innerHTML = `
                <div style="display:flex;flex-direction:column;justify-content:center;align-items:center;height:100%;text-align:center;padding:20px;">
                    <i class="fas fa-check-circle" style="font-size:3em;color:#28a745;margin-bottom:16px;"></i>
                    <h2 style="color:#fff;margin-bottom:8px;">Setup Complete!</h2>
                    <p style="color:#aaa;max-width:480px;margin-bottom:24px;">
                        Your configuration has been saved.
                        Please <strong style="color:#ffc107;">restart EOS Connect</strong> for the new settings to take effect.
                    </p>
                    <div style="display:flex;gap:12px;flex-wrap:wrap;justify-content:center;">
                        <button onclick="closeFullScreenOverlay();"
                                style="background:#4a9eff;color:#fff;border:none;border-radius:8px;padding:10px 24px;font-size:0.95em;cursor:pointer;display:inline-flex;align-items:center;gap:8px;">
                            <i class="fas fa-times"></i> Close
                        </button>
                        <button onclick="closeFullScreenOverlay(); if(typeof showConfigurationMenu === 'function') showConfigurationMenu();"
                                style="background:rgba(74,158,255,0.15);color:#fff;border:2px solid rgba(74,158,255,0.4);border-radius:8px;padding:10px 24px;font-size:0.95em;cursor:pointer;display:inline-flex;align-items:center;gap:8px;">
                            <i class="fas fa-cog"></i> Open Configuration
                        </button>
                    </div>
                </div>
            `;

        } catch (err) {
            console.error("[SetupWizard] Save error:", err);
            if (nextBtn) {
                nextBtn.disabled = false;
                nextBtn.innerHTML = '<i class="fas fa-check"></i> Finish';
            }
            // Show error in a non-destructive way
            const errArea = document.getElementById("wizard-step-content");
            if (errArea) {
                const existing = errArea.querySelector(".wizard-save-error");
                if (existing) {
                    existing.remove();
                }
                const errDiv = document.createElement("div");
                errDiv.className = "wizard-save-error";
                errDiv.style.cssText = "color:#dc3545;background:rgba(220,53,69,0.1);border-radius:8px;padding:12px;margin-top:12px;";
                errDiv.innerHTML = `<i class="fas fa-exclamation-triangle"></i> ${this._escapeHtml(String(err.message || err))}`;
                errArea.appendChild(errDiv);
            }
        }
    }

    // ── Helper methods ──────────────────────────────────────────

    /**
     * Get the getting_started-level fields for a step.
     * @param {object} step - Step definition
     * @returns {Array} Array of field definitions
     */
    _getStepFields(step) {
        if (!this.schema || !step.sections || step.sections.length === 0) {
            return [];
        }
        return this.schema.filter(
            f => step.sections.includes(f.section) && f.level === "getting_started"
        );
    }

    /**
     * Check if a field's depends_on conditions are met.
     * @param {object} f - Field definition
     * @returns {boolean} True if dependency is met
     */
    _isDependencyMet(f) {
        if (!f.depends_on) {
            return true;
        }
        for (const [depKey, allowed] of Object.entries(f.depends_on)) {
            const current = this.values[depKey];
            if (allowed === "!empty") {
                if (!current || current === "") {
                    return false;
                }
            } else if (Array.isArray(allowed)) {
                // Compare loosely — schema may store bools/strings
                const match = allowed.some(v => String(v) === String(current));
                if (!match) {
                    return false;
                }
            }
        }
        return true;
    }

    /**
     * Check if a field is required (has non-empty validation constraints).
     * @param {object} f - Field definition
     * @returns {boolean} True if effectively required
     */
    _isRequired(f) {
        const v = f.validation || {};
        return Boolean(v.required) || Boolean(v.pattern);
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


// ── Global instance & functions ─────────────────────────────────
let setupWizard;

/**
 * Show the Setup Wizard overlay (called from menu or auto-trigger).
 */
function showSetupWizard() {
    if (!setupWizard) {
        setupWizard = new SetupWizard();
    }
    setupWizard.show();
}

/**
 * Check wizard status on page load and auto-trigger if pending.
 * Guarded so it only fires once — the 1 s init() loop must not
 * re-trigger the wizard after the user has started interacting.
 */
let _wizardCheckDone = false;
async function checkWizardStatus() {
    if (_wizardCheckDone) {
        return;
    }
    _wizardCheckDone = true;
    try {
        const res = await fetch("api/config/wizard-status");
        if (!res.ok) {
            _wizardCheckDone = false;   // allow retry on transient failure
            return;
        }
        const status = await res.json();
        if (status.pending) {
            showSetupWizard();
        }
    } catch {
        _wizardCheckDone = false;       // allow retry on transient failure
    }
}
