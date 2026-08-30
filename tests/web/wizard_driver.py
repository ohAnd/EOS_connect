"""
Page-object helpers for driving the Setup Wizard in a browser.

The wizard generates its DOM ids from the schema key by replacing dots with dashes
(``_renderField``, ``wizard.js:286-321``), so every field is reachable without a test
hook: the row is ``#wiz-field-{cssKey}``, the input ``#wiz-{cssKey}``, the error slot
``#wiz-err-{cssKey}``.  Navigation is ``#wiz-back`` / ``#wiz-next`` / ``#wiz-skip``.

Visibility is the subtle part.  A field whose ``depends_on`` is unmet is not removed —
``_renderField`` tags it ``.wizard-conditional.hidden`` and the stylesheet collapses it
to ``max-height: 0`` (``wizard.css:245``).  ``visible_fields`` therefore measures the
rendered box rather than trusting the markup, which is the only way to tell a step that
is genuinely empty from one that merely looks it.
"""

# Step ids in the order the wizard walks them (``wizard.js:26-98``).  Tests assert
# against this rather than hard-coding indices, so a reorder shows up in one place.
STEP_IDS = [
    "welcome",
    "eos",
    "evcc",
    "data_source",
    "inverter",
    "battery",
    "load",
    "price",
    "pv",
    "review",
]


def css_key(key):
    """``battery.capacity_wh`` -> ``battery-capacity_wh``, the wizard's id convention."""
    return key.replace(".", "-")


# ── State ───────────────────────────────────────────────────────


def current_step_id(page):
    """The id of the step on screen, read from the wizard instance itself."""
    return page.evaluate("() => setupWizard.steps[setupWizard.currentStep].id")


def step_title(page):
    """The heading the user sees, or '' on the welcome/review steps which have none."""
    el = page.query_selector(".wizard-step-title")
    return el.inner_text().strip() if el else ""


def step_description(page):
    """The sentence under the heading explaining what the step is for."""
    el = page.query_selector(".wizard-step-description")
    return el.inner_text().strip() if el else ""


def rendered_fields(page):
    """Every field the step emitted, visible or not, in DOM order."""
    return page.eval_on_selector_all(
        "#wizard-step-content .wizard-field",
        "els => els.map(e => e.getAttribute('data-key'))",
    )


def visible_fields(page):
    """
    The fields a user can actually see and fill in.

    Measured, not inferred: a collapsed ``.wizard-conditional.hidden`` row still exists
    in the DOM with a ``data-key``, so counting markup would report a blank step as full.
    """
    return page.eval_on_selector_all(
        "#wizard-step-content .wizard-field",
        """els => els.filter(e => {
            const r = e.getBoundingClientRect();
            return r.height > 0 && r.width > 0;
        }).map(e => e.getAttribute('data-key'))""",
    )


def empty_state_text(page):
    """The 'nothing to configure' message, or '' when the step rendered fields."""
    content = page.inner_text("#wizard-step-content").strip()
    return content if "No fields to configure" in content else ""


def field_error(page, key):
    """The validation message under one field, or ''."""
    el = page.query_selector(f"#wiz-err-{css_key(key)}")
    return el.inner_text().strip() if el else ""


def choices(page, key):
    """The options of a select, as ``[(value, disabled), ...]``."""
    return page.eval_on_selector_all(
        f"#wiz-{css_key(key)} option",
        "els => els.map(e => [e.value, e.disabled])",
    )


def option_labels(page, key):
    """The visible text of a select's options, in order."""
    return page.eval_on_selector_all(
        f"#wiz-{css_key(key)} option",
        "els => els.map(e => e.textContent.trim())",
    )


def field_value(page, key):
    """What a field currently holds, as the browser sees it."""
    return page.eval_on_selector(f"#wiz-{css_key(key)}", "el => el.value")


def review_rows(page):
    """The review step's summary as ``{section title: {label: value}}``."""
    return page.evaluate(
        """() => {
            const out = {};
            for (const sec of document.querySelectorAll('.wizard-review-section')) {
                const title = sec.querySelector('h4').innerText.trim();
                const rows = {};
                for (const r of sec.querySelectorAll('.wizard-review-row')) {
                    rows[r.querySelector('.label').innerText.trim()] =
                        r.querySelector('.value').innerText.trim();
                }
                out[title] = rows;
            }
            return out;
        }"""
    )


# ── Interaction ─────────────────────────────────────────────────


def set_field(page, key, value):
    """Fill one field, firing the events the wizard listens for."""
    selector = f"#wiz-{css_key(key)}"
    page.wait_for_selector(selector, state="attached")
    kind = page.eval_on_selector(
        selector, "e => e.tagName === 'SELECT' ? 'select' : e.type"
    )
    if kind == "select":
        page.select_option(selector, str(value))
    elif kind == "checkbox":
        page.set_checked(selector, bool(value))
    else:
        page.fill(selector, str(value))
        # ``fill`` emits input but not change; the wizard re-evaluates conditional
        # visibility on change only (``_bindStepEvents``, wizard.js:593).
        page.dispatch_event(selector, "change")


def next_step(page):
    """Click Next and wait for the step to actually turn over."""
    before = current_step_id(page)
    page.click("#wiz-next")
    page.wait_for_function(
        "before => setupWizard.steps[setupWizard.currentStep].id !== before",
        arg=before,
    )
    page.wait_for_selector("#wizard-step-content")


def next_step_expecting_block(page):
    """Click Next when validation should refuse; returns True if the step held."""
    before = current_step_id(page)
    page.click("#wiz-next")
    page.wait_for_timeout(200)
    return current_step_id(page) == before


def skip_step(page):
    """Click Skip on an optional step and wait for the next one."""
    before = current_step_id(page)
    page.click("#wiz-skip")
    page.wait_for_function(
        "before => setupWizard.steps[setupWizard.currentStep].id !== before",
        arg=before,
    )


def back_step(page):
    """Click Back and wait for the previous step to render."""
    before = current_step_id(page)
    page.click("#wiz-back")
    page.wait_for_function(
        "before => setupWizard.steps[setupWizard.currentStep].id !== before",
        arg=before,
    )


def finish(page):
    """Click Finish on the review step and wait for the wizard to report an outcome."""
    page.click("#wiz-next")
    page.wait_for_selector(
        "#full_screen_content:has-text('Setup Complete'), .wizard-save-error"
    )


def finished_successfully(page):
    """Whether the wizard claims the save worked."""
    return page.is_visible("#full_screen_content:has-text('Setup Complete')")


def save_error_text(page):
    """The message shown when the save was refused, or ''."""
    el = page.query_selector(".wizard-save-error")
    return el.inner_text().strip() if el else ""
