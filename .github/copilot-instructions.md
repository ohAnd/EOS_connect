# GitHub Copilot Instructions for EOS Connect

## Project Guidelines

### Icon Usage

- **Always use FontAwesome icons (free tier only)** for all documentation and web interfaces
- **Never use emoji icons** - they have been replaced with FontAwesome for consistency and professionalism
- The main application icon is located in `/docs/assets/images/icon.png` and `/docs/assets/images/logo.png`
- FontAwesome CDN: https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.7.2/css/all.min.css

### Design Style

- Follow the dark theme established in `src/web/css/style.css`
- Color scheme:
  - Primary background: `rgb(54, 54, 54)`
  - Secondary background: `rgb(78, 78, 78)`
  - Accent color: `#4a9eff`
  - Border radius: `10px`
- Maintain responsive design patterns

### Documentation

#### Structure

- GitHub Pages documentation is in `/docs` folder
- Structure: 4 main sections (what-is, user-guide, advanced, developer)
- Use HTML for documentation pages (better styling control than Markdown)
- Keep README.md and CONFIG_README.md concise with links to full docs

#### Documentation Update Workflow

**When preparing to commit (NEVER stage changed files and commit automatically - always review changes first and ask for confirmation):**

1. **Update README.md** - Minimal info only, focus on quick start + links to GitHub Pages
2. **Update src/CONFIG_README.md** - Essential configuration overview + links to full docs
3. **Update GitHub Pages** (`/docs` folder) - Complete, detailed documentation
   - Always write from **user perspective** (except developer section)
   - Main focus: **"Easy entry for new and existing users"**
   - Keep all pages current with latest features and changes
   - Use clear, practical examples

#### Documentation Perspective

- **what-is/**, **user-guide/**, **advanced/**: Write for end users (clear, accessible language)
- **developer/**: Write for contributors (technical details, architecture)
- All documentation should help users quickly understand and use EOS Connect
- Avoid jargon unless necessary; explain technical concepts simply

### Project Role Clarity

- **EOS Connect is an integration and control platform**, NOT an optimizer
- The optimization calculations are performed by external servers:
  - Akkudoktor EOS Server (https://github.com/Akkudoktor-EOS/EOS)
  - EVopt (https://github.com/thecem/hassio-evopt)
- Always clarify this distinction in documentation and code comments

### Code Style

- Follow existing Python conventions in the codebase
- Use type hints where appropriate
- Include docstrings for classes and functions
- Follow pylint recommendations for formatting

### Code Changes & Documentation Alignment

**MANDATORY: Every code change, new feature, or bugfix MUST be reflected in documentation**

When making ANY code changes:

1. **Identify Documentation Impact**: Determine which doc sections are affected
   - New features → Update what-is, user-guide, and advanced pages
   - Configuration changes → Update user-guide/configuration.html
   - API changes → Update advanced/index.html (REST API & MQTT sections)
   - Bug fixes → Update troubleshooting in user-guide if user-facing

2. **Update All Affected Pages**: Changes must be synchronized across:
   - `/docs` GitHub Pages (primary documentation)
   - `README.md` (if quick start or core features affected)
   - `src/CONFIG_README.md` (if configuration parameters changed) - NOTE: This file is being deprecated, integrate changes into README.md instead

3. **Maintain Accuracy**: Documentation must match actual code behavior
   - Verify API endpoint responses match code
   - Confirm MQTT topic names and payloads match implementation
   - Validate configuration parameter names, types, and valid values
   - Update examples to reflect current best practices

4. **Version Consistency**: When `src/version.py` is updated, ensure version display is current on all doc pages

**Failure to update documentation is considered incomplete work**

### Commit Preparation

- **NEVER commit automatically** - only prepare changes for user review
- When asked to "prepare to commit", ensure documentation is up-to-date:
  1. Update README.md (minimal, with links)
  2. Update src/CONFIG_README.md (essential info, with links)
  3. Update GitHub Pages documentation (complete details)
- Present a summary of changes for user to review before committing

### Testing Phase Documentation

- **ENERGYFORECAST_TESTING.md**: Temporary file for develop branch testing
  - Contains Smart Price Prediction testing guide
  - **MUST BE DELETED** when merging to main
  - Full documentation already exists in `/docs/user-guide/configuration.html#energyforecast`
  - Purpose: Provide accessible docs while feature is on develop (GitHub Pages shows main only)
  - **Reminder**: Check for and remove any similar `*_TESTING.md` files before merging features to main

### Config Schema Maintenance

- Every new config field **MUST** be added to `src/config_web/schema.py`
- Schema fields: `key`, `type`, `default`, `section`, `level`, `label`, `description`, `help_url`, `validation`, `depends_on`, `hot_reload`, `display_group`
- After schema changes, run: `python scripts/export_config_schema.py`
- The Config Schema is the **SINGLE SOURCE OF TRUTH** for field metadata
- Web UI and GitHub Pages docs both consume the exported JSON (`docs/assets/data/config_schema.json`)
- New fields must specify a `level`: `getting_started`, `standard`, or `expert`
- New experimental features should use the label `"experimental"`
- Hot-reloadable fields (applied without restart) must set `hot_reload=True` and have corresponding logic in `src/config_web/hot_reload.py`

### Testing

- Tests are located in `/tests` folder
- Mirror the source structure in test organization
- Use pytest for all testing
